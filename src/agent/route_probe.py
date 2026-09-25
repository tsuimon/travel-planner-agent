"""Read-only route and service-window evidence, including rejected itineraries."""

import asyncio
from datetime import datetime, timedelta

from src.agent.intelligent import IntelligentPlanner
from src.search.evidence import normalize_transit


SOURCE = "https://restapi.amap.com/v5/direction/transit/integrated"


async def probe_route(
    planner: IntelligentPlanner, origin: str, destination: str, at: datetime, line_filter: str | None = None
) -> dict:
    """Keep line-level evidence even when the complete route misses a later service.

    Line terminal departure clocks are never presented as boarding-station clocks.
    Backward estimates are explicitly unverified until queried at that departure.
    """
    if at.tzinfo is None:
        raise ValueError("查询时间必须包含时区")
    a, b = await asyncio.gather(planner.resolve(origin), planner.resolve(destination))
    if not a or not b:
        return {"error": "地点未能定位", "origin": origin, "destination": destination}
    body = await planner.call(
        "amap_route",
        dict(
            origin=a["location"],
            destination=b["location"],
            city1=a["citycode"],
            city2=b["citycode"],
            at=at.isoformat(),
            strategy="1",
        ),
    )
    if not body:
        return {"error": "地图查询暂不可用"}
    routes, rejected = normalize_transit(body, origin, destination, at)
    windows, seen = [], set()
    for raw in body.get("route", {}).get("transits", [])[:5]:
        for index, segment in enumerate(raw.get("segments", [])):
            for line in (segment.get("bus") or {}).get("buslines", [])[:2]:
                name = str(line.get("name") or "")
                station = (line.get("departure_stop") or {}).get("name")
                if (name, station) in seen:
                    continue
                seen.add((name, station))
                # Validate the access prefix even if a later interchange fails.
                prefix = {"status": "1", "route": {"transits": [{"segments": raw["segments"][: index + 1]}]}}
                prefix_routes, _ = normalize_transit(prefix, origin, destination, at)
                boarding = next(
                    (
                        s
                        for r in prefix_routes
                        for s in reversed(r.steps)
                        if s.name == name and s.origin == station
                    ),
                    None,
                )
                windows.append(
                    dict(
                        line=name,
                        boarding_station=station,
                        alighting_station=(line.get("arrival_stop") or {}).get("name"),
                        station_last=line.get("station_end_time") or None,
                        line_terminal_last=line.get("end_time") or None,
                        estimated_boarding=boarding.departure.isoformat() if boarding else None,
                        last_boarding_at=boarding.last_boarding.isoformat()
                        if boarding and boarding.last_boarding
                        else None,
                    )
                )
    if line_filter:
        # Filtering provider results is not conversational intent routing.
        needle = line_filter.replace("地铁", "").replace(" ", "")
        windows.sort(key=lambda row: needle not in row["line"].replace(" ", ""))
    checked = []
    for route in routes[:2]:
        steps = []
        for step in route.steps:
            row = dict(
                name=step.name,
                station=step.origin,
                departure=step.departure.isoformat(),
                arrival=step.arrival.isoformat(),
            )
            if step.last_boarding:
                row["station_last"] = step.last_boarding.isoformat()
                # Access time includes preceding walking, rides and transfer buffers.
                candidate = step.last_boarding - (step.departure - route.departure) - timedelta(minutes=10)
                row["origin_departure_estimate_needs_requery"] = candidate.isoformat()
            steps.append(row)
        checked.append(
            dict(departure=route.departure.isoformat(), arrival=route.arrival.isoformat(), steps=steps)
        )
    return dict(
        origin=origin,
        destination=destination,
        queried_departure=at.isoformat(),
        source=SOURCE,
        focus_line=line_filter,
        windows=windows[:8],
        checked_routes=checked,
        rejected=rejected,
        limitations="运营时间为地图参考；线路始发站末班不等于上车站末班；赶上一条线不等于后续换乘可行。倒推时刻须重新查询，非保证赶上。",
        location_notes=planner.report.assumptions,
    )


def describe_probe(value: dict) -> str:
    """Render factual clocks directly from evidence, also useful when the LLM times out."""
    if value.get("error"):
        return value["error"] + "，暂不能确认末班和接续时刻。"

    def display_time(clock: str) -> str:
        try:
            return datetime.fromisoformat(clock).strftime("%m-%d %H:%M")
        except ValueError:
            return clock[:2] + ":" + clock[2:] if len(clock) == 4 and clock.isdigit() else clock

    parts = [
        f"按 {display_time(value['queried_departure'])} 从{value['origin']}去{value['destination']}查询："
    ]
    details = []
    for row in value.get("windows", [])[:8]:
        time = row.get("last_boarding_at") or row.get("station_last")
        time = display_time(time) if time else None
        clock = f"上车站末班参考 {time}" if time else "接口未提供该上车站末班"
        if not time and row.get("line_terminal_last"):
            clock += f"（线路始发末班 {row['line_terminal_last']}，不能直接用于赶车）"
        focus = (value.get("focus_line") or "").replace("地铁", "")
        target = parts if focus in row["line"] else details
        target.append(f"- **{row['line']}**：{row['boarding_station']} 上车，{clock}。")
        if row.get("estimated_boarding"):
            target.append(f"预计 {display_time(row['estimated_boarding'])} 在该站上车（接驳耗时估算）。")
            if row.get("last_boarding_at"):
                margin = (
                    datetime.fromisoformat(row["last_boarding_at"])
                    - datetime.fromisoformat(row["estimated_boarding"])
                ).total_seconds() // 60
                if margin >= 10:
                    target.append(f"到这条线路的接驳估算比末班早约 {int(margin)} 分钟；仍需检查后面的换乘。")
                elif margin >= 0:
                    target.append("到该站距离末班不足10分钟，余量偏小，建议提前并重新核对路线。")
    routes = value.get("checked_routes", [])
    if routes:
        parts.append("该次查询中通过时间衔接校验的候选：")
        for route in routes:
            parts.append(
                f"- {display_time(route['departure'])} 出发，预计 {display_time(route['arrival'])} 到达。"
            )
    else:
        parts.append("该出发时间下尚无通过完整时间衔接校验的路线；单条线路的运营时间不能证明全程可行。")
    if details:
        parts.append(
            "<details><summary>其他线路的查询依据</summary>\n\n" + "\n\n".join(details) + "\n\n</details>"
        )
    parts += [value["limitations"], f"[高德路线数据来源]({value['source']})"]
    return "\n\n".join(parts)
