"""Select an alighting station by feasible metro access plus priced taxi egress."""

import math
from datetime import datetime, timedelta

from src.domain import Mode, TZ
from src.errors import DeadlineExceeded
from src.search.evidence import RouteEvidence, RouteStep, cents, normalize_transit


def straight_distance(a: str, b: str) -> float:
    lon1, lat1 = map(float, a.split(","))
    lon2, lat2 = map(float, b.split(","))
    return math.hypot((lon1 - lon2) * math.cos(math.radians(lat1)), lat1 - lat2)


def usable_metro(route: RouteEvidence) -> bool:
    """Reject unverified last trains and next-morning waiting disguised as a night route."""
    metro = [s for s in route.steps if s.mode == Mode.metro]
    if not metro or any(s.mode not in {Mode.walk, Mode.metro} for s in route.steps):
        return False
    if sum(s.distance_m for s in route.steps if s.mode == Mode.walk) > 5000:
        return False
    cursor = route.departure
    for step in route.steps:
        if step.departure - cursor > timedelta(minutes=30):
            return False
        if step.mode == Mode.metro:
            if step.last_boarding is None or step.departure + timedelta(minutes=2) > step.last_boarding:
                return False
        cursor = step.arrival
    return True


def taxi_route(body: dict, origin: str, destination: str, at: datetime) -> RouteEvidence | None:
    """The route-level taxi_cost applies to the default driving path, not every alternative."""
    raw = body.get("route") or {}
    paths = raw.get("paths") or []
    if not paths:
        return None
    path = paths[0]
    try:
        seconds = int((path.get("cost") or {}).get("duration") or 0)
        length = int(path.get("distance") or 0)
        if seconds <= 0:
            return None
    except (TypeError, ValueError):
        return None
    arrival = at + timedelta(seconds=seconds)
    return RouteEvidence(
        origin=origin,
        destination=destination,
        departure=at,
        arrival=arrival,
        cost_cents=cents(raw.get("taxi_cost")),
        queried_at=datetime.now(TZ),
        source="https://restapi.amap.com/v5/direction/driving",
        steps=[
            RouteStep(
                mode=Mode.taxi,
                name="打车接驳",
                origin=origin,
                destination=destination,
                departure=at,
                arrival=arrival,
                distance_m=length,
            )
        ],
        warnings=[
            "打车费用是高德出租车估价；夜间加价、网约车动态价格及实际等车时间未核实，驾车耗时按查询时路况估算"
        ],
    )


class NightTransferSearch:
    """Expand map-returned lines, then re-query each station at the actual departure time.

    Stations are spatially shortlisted (max 8); only a fresh, time-validated route
    proves reachability. No schedules are interpolated from the station list.
    """

    def __init__(self, planner):
        self.planner = planner
        self.tested = 0

    async def routes(self, origin: str, destination: str, at: datetime, a: dict, b: dict):
        p = self.planner
        args = dict(
            origin=a["location"],
            destination=b["location"],
            city1=a["citycode"],
            city2=b["citycode"],
            at=at.isoformat(),
        )
        results = []
        try:
            # Observe useful lines even when the full destination route misses its final train.
            seeds = []
            for strategy in ("1", "8"):
                body = await p.call("amap_route", {**args, "strategy": strategy})
                if body:
                    seeds.extend((body.get("route") or {}).get("transits", [])[:5])
            stations, first_lines = {}, {}
            for route in seeds:
                first = True
                for segment in route.get("segments", []):
                    for line in (segment.get("bus") or {}).get("buslines", [])[:1]:
                        if "地铁" not in str(line.get("type", "")):
                            continue
                        end = line.get("arrival_stop") or {}
                        if end.get("location") and end.get("name"):
                            stations[end["name"]] = end
                        if first and line.get("id"):
                            first_lines[line["id"]] = line.get("departure_stop", {})
                            first = False
            # Continuing on a boardable line can beat every transfer offered for the final destination.
            extended = {}
            for line_id, boarding in list(first_lines.items())[:2]:
                body = await p.call("amap_line", {"id": line_id})
                for line in (body or {}).get("lines", []):
                    if str(line.get("status")) not in {"1", "None"}:
                        continue
                    stops = line.get("busstops") or []
                    index = next(
                        (
                            i
                            for i, s in enumerate(stops)
                            if s.get("id") == boarding.get("id")
                            or (
                                s.get("name") == boarding.get("name")
                                and s.get("location")
                                and boarding.get("location")
                                and straight_distance(s["location"], boarding["location"]) < 0.003
                            )
                        ),
                        None,
                    )
                    if index is None:
                        continue
                    onward = [s for s in stops[index + 1 :] if s.get("location") and s.get("name")]
                    for station in sorted(
                        onward, key=lambda s: straight_distance(s["location"], b["location"])
                    )[:2]:
                        extended[station["name"]] = station
            # Reserve expansion candidates so main-route transfer stations cannot crowd them out.
            targets = list(extended.values())
            for s in sorted(stations.values(), key=lambda s: straight_distance(s["location"], b["location"])):
                if s["name"] not in extended and len(targets) < 8:
                    targets.append(s)
            for station in targets:
                self.tested += 1
                prefix_body = await p.call(
                    "amap_route", {**args, "destination": station["location"], "strategy": "8"}
                )
                if not prefix_body:
                    continue
                try:
                    prefixes, errors = normalize_transit(prefix_body, origin, station["name"], at)
                except (ValueError, TypeError):
                    p.reject("地铁接驳响应格式错误")
                    continue
                for error in errors:
                    p.reject("地铁接驳已过末班" if error == "outside_operating_hours" else "接驳数据不足")
                valid = [x for x in prefixes if usable_metro(x)]
                if not valid:
                    p.reject("下车站不可及时到达或末班资料不足")
                    continue
                # Price one suffix per station; its traffic estimate is not departure-time-specific.
                body = await p.call("amap_route", {**args, "origin": station["location"], "mode": "drive"})
                for prefix in valid:
                    ready = prefix.arrival + timedelta(minutes=10)
                    taxi = taxi_route(body or {}, station["name"], destination, ready)
                    if not taxi:
                        continue
                    known = prefix.cost_cents is not None and taxi.cost_cents is not None
                    route = RouteEvidence(
                        origin=origin,
                        destination=destination,
                        departure=at,
                        arrival=taxi.arrival,
                        cost_cents=prefix.cost_cents + taxi.cost_cents if known else None,
                        steps=[*prefix.steps, *taxi.steps],
                        queried_at=datetime.now(TZ),
                        warnings=[
                            *taxi.warnings,
                            f"自动选择在{station['name']}下车；出站与等车暂留10分钟",
                            f"地铁参考¥{prefix.cost_cents / 100:.2f} + 打车估价¥{taxi.cost_cents / 100:.2f}"
                            if known
                            else "地铁或打车费用缺失，不能确认总费用",
                            "地铁时刻含每次候车5分钟估计，另要求距末班至少2分钟；末班时刻以车站当天公告为准",
                            "打车数据来源：https://restapi.amap.com/v5/direction/driving",
                        ],
                    )
                    results.append(route)
            p.report.assumptions.append(
                f"程序搜索了{self.tested}个下车站，按可衔接地铁票价加打车估价选站；最低价仅指本轮候选"
            )
        except (DeadlineExceeded, TimeoutError):
            p.report.data_gaps.append("选站查询达到时限，保留已完成候选")
        return results
