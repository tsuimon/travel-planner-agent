"""Bounded whole-trip decisions using live route evidence and explicit assumptions."""

import asyncio
import math
from datetime import datetime, timedelta
from pydantic import Field

from src.domain import Activity, ItineraryDraft, Model, Mode, Preferences, TZ
from src.errors import DeadlineExceeded
from src.search.evidence import RouteEvidence, RouteStep, normalize_transit
from src.search.night_transfer import NightTransferSearch


class JourneyOption(Model):
    location: str
    ready: datetime
    routes: list[RouteEvidence] = Field(default_factory=list)
    activities: list[Activity] = Field(default_factory=list)
    transport_cents: int = 0
    activity_cents: int = 0
    unknown_cost: bool = False
    decisions: list[str] = Field(default_factory=list)


class PlanningReport(Model):
    departure_at: datetime | None = None
    arrive_by: datetime | None = None
    arrival_priority: bool = False
    options: list[JourneyOption] = Field(default_factory=list)
    completed_stops: int = 0
    total_stops: int = 0
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    rejected: dict[str, int] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
    calls: int = 0
    min_over_budget_cents: int | None = None
    budget_cents: int | None = None
    budget_scope: str = "transport"
    tradeoffs: list[dict] = Field(default_factory=list)
    metro_then_taxi: bool = False


def distance(a: str, b: str) -> float:
    lon1, lat1 = map(float, a.split(","))
    lon2, lat2 = map(float, b.split(","))
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000 * math.hypot(x, y)


class IntelligentPlanner:
    """Choose stops and departure branches; retain useful partial work on data gaps."""

    def __init__(self, registry, deadline: float) -> None:
        self.registry, self.deadline = registry, deadline
        self.report = PlanningReport()
        self.positions: dict[str, dict] = {}
        self.venues: set[str] = set()
        self.preferences = Preferences()
        self.metro_then_taxi = False
        self.arrival_priority = False

    async def arrival_routes(self, draft: ItineraryDraft) -> list[RouteEvidence]:
        """Re-query provider after moving departure backwards from a hard arrival deadline.

        A forward quote is only a duration hint. Every proposed later departure
        is queried again; no timetable is shifted or asserted globally optimal.
        """
        start, deadline = draft.depart_after, draft.arrive_by
        destination = draft.stops[0].locations[0]
        seed = await self.routes(draft.origin, destination, start)
        if not seed:
            seed = await self.routes(draft.origin, destination, start, "8")
        values = list(seed)
        tried = {start}
        for route in sorted(seed, key=lambda x: x.arrival)[:3]:
            # Refine twice: a midnight seed can include hours waiting for the first service.
            # Re-querying at a daytime departure removes that wait before estimating the next clock.
            for _ in range(2):
                proposed = deadline - (route.arrival - route.departure) - timedelta(minutes=15)
                proposed = min(proposed, draft.depart_before or proposed)
                if proposed < start or proposed in tried:
                    break
                tried.add(proposed)
                checked = await self.routes(draft.origin, destination, proposed, "8")
                values.extend(checked)
                if not checked:
                    break
                route = min(checked, key=lambda x: x.arrival)
        self.report.assumptions.append(
            "按到达期限倒推出发候选，预留15分钟余量并重新查路线；不保证这是最晚可行出发时刻"
        )
        return values

    def reject(self, reason: str) -> None:
        self.report.rejected[reason] = self.report.rejected.get(reason, 0) + 1

    async def call(self, name: str, params: dict):
        if self.report.calls >= 28:
            self.reject("搜索次数上限")
            return None
        self.report.calls += 1
        result = await self.registry.call(name, params, self.deadline)
        if not result.success:
            self.reject("部分地图查询失败")
            return None
        return result.data

    async def resolve(self, name: str) -> dict | None:
        if name not in self.positions:
            if name in self.venues:
                found = await self.call("amap_places", {"keywords": name + " 体育场"})
                places = found.get("places", []) if found else []
                if places:
                    city = await self.resolve(places[0]["city"]) if not places[0].get("citycode") else None
                    citycode = places[0].get("citycode") or (city or {}).get("citycode")
                    if not citycode:
                        return None
                    self.positions[name] = dict(
                        location=places[0]["location"],
                        citycode=citycode,
                        formatted_address=places[0]["name"],
                        level="兴趣点",
                    )
                    self.report.assumptions.append(
                        f"{name}按{places[0]['name']}测算；以演出票上的场馆和入口为准"
                    )
                    return self.positions[name]
            # A landmark alias must not silently resolve to an unrelated same-name shop.
            aliases = {"鸟巢": "北京国家体育场", "北京鸟巢": "北京国家体育场"}
            address = aliases.get(name, name)
            result = await self.call("amap_geocode", {"address": address})
            values = result.get("locations", []) if result else []
            if not values:
                self.reject("地址没有坐标")
                return None
            self.positions[name] = values[0]
            if address != name:
                self.report.assumptions.append(f"{name}按{address}定位，目的地按地图匹配的校区/地址测算")
            if len(values) > 1 or values[0].get("level") in {"道路", "城市", "区县", "市"}:
                note = f"{name}暂用地图匹配点：{values[0]['formatted_address']}，具体出入口可能影响接驳"
                if note not in self.report.assumptions:
                    self.report.assumptions.append(note)
        return self.positions[name]

    async def routes(
        self, origin: str, destination: str, at: datetime, strategy: str = "1"
    ) -> list[RouteEvidence]:
        a, b = await asyncio.gather(self.resolve(origin), self.resolve(destination))
        if not a or not b:
            return []
        if self.metro_then_taxi:
            if a["citycode"] != b["citycode"]:
                self.report.data_gaps.append("先地铁再打车的自动选站目前用于同城行程")
                return []
            return await NightTransferSearch(self).routes(origin, destination, at, a, b)
        args = dict(
            origin=a["location"],
            destination=b["location"],
            city1=a["citycode"],
            city2=b["citycode"],
            at=at.isoformat(),
            strategy=strategy,
        )
        body = await self.call("amap_route", args)
        routes = []
        if body:
            try:
                routes, rejected = normalize_transit(body, origin, destination, at)
                reasons = {
                    "rail_connection_too_short": "赶不上列车或进站时间不足",
                    "outside_operating_hours": "公交/地铁已过末班",
                    "rail_clock_conflict": "供应商列车时刻互相冲突",
                }
                for error in rejected:
                    self.reject(reasons.get(error, "路线字段不足或不支持"))
            except (ValueError, TypeError):
                self.reject("路线响应格式错误")
        if a["citycode"] == b["citycode"] and distance(a["location"], b["location"]) <= 3000:
            walked = await self.call("amap_route", {**args, "mode": "walk"})
            if walked:
                for p in (walked.get("route") or {}).get("paths", [])[:1]:
                    seconds = int((p.get("cost") or {}).get("duration") or 0)
                    length = int(p.get("distance") or 0)
                    if seconds > 0 and length <= 5000:
                        arrive = at + timedelta(seconds=seconds)
                        routes.append(
                            RouteEvidence(
                                origin=origin,
                                destination=destination,
                                departure=at,
                                arrival=arrive,
                                cost_cents=0,
                                steps=[
                                    RouteStep(
                                        mode=Mode.walk,
                                        name=f"步行{length}米",
                                        origin=origin,
                                        destination=destination,
                                        departure=at,
                                        arrival=arrive,
                                        distance_m=length,
                                    )
                                ],
                                source="https://restapi.amap.com/v5/direction/walking",
                                queried_at=datetime.now(TZ),
                                warnings=["步行时长为估算；演出散场人流可能延长接驳"],
                            )
                        )
        return routes

    async def choices(self, draft: ItineraryDraft, index: int) -> list[str]:
        stop = draft.stops[index]
        generic = {"海底捞", "餐厅", "饭店", "酒店"}
        if stop.locations and not any(p in generic for p in stop.locations):
            return stop.locations[:2]
        nearby = draft.stops[index - 1].locations if index else [draft.origin or ""]
        result = await self.call(
            "amap_places", {"keywords": " ".join([*nearby, *(stop.locations or [stop.label])])[:80]}
        )
        names = [p["name"] for p in result.get("places", [])[:2]] if result else []
        if result:
            for p in result.get("places", [])[:2]:
                city = await self.resolve(p["city"]) if not p.get("citycode") else None
                citycode = p.get("citycode") or (city or {}).get("citycode")
                if citycode:
                    self.positions[p["name"]] = dict(
                        location=p["location"],
                        citycode=citycode,
                        formatted_address=p["name"],
                        level="兴趣点",
                    )
                if p.get("opening_hours"):
                    self.report.assumptions.append(
                        f"{p['name']}：地图营业时间为{p['opening_hours']}，演出当天需向门店确认"
                    )
        if names:
            self.report.assumptions.append(
                f"{stop.label}未指定门店，主动比较：{'、'.join(names)}；营业与排队尚未核实"
            )
        return names

    def shortlist(self, values: list[JourneyOption]) -> list[JourneyOption]:
        """Retain cheap and early labels so one cheap first leg cannot erase an onward connection."""
        unique = {v.model_dump_json(): v for v in values}
        if self.arrival_priority:
            return sorted(
                unique.values(),
                key=lambda v: (
                    v.unknown_cost,
                    v.transport_cents if self.preferences.budget_preference == "economy" else 0,
                    -v.routes[0].departure.timestamp(),
                    v.transport_cents,
                ),
            )[:4]
        cheap = sorted(
            unique.values(), key=lambda v: (v.unknown_cost, v.transport_cents + v.activity_cents, v.ready)
        )
        early = sorted(unique.values(), key=lambda v: (v.ready, v.transport_cents + v.activity_cents))
        if self.preferences.budget_preference == "fast":
            cheap, early = early, cheap
        kept = []
        for pair in zip(cheap, early):
            for item in pair:
                if item not in kept:
                    kept.append(item)
            if len(kept) >= 4:
                break
        return kept[:4]

    async def plan(self, draft: ItineraryDraft) -> PlanningReport:
        r = self.report
        self.preferences = draft.preferences
        self.metro_then_taxi = r.metro_then_taxi = draft.metro_then_taxi
        self.arrival_priority = r.arrival_priority = draft.arrival_priority
        r.arrive_by = draft.arrive_by
        r.assumptions.extend(draft.assumptions)
        if draft.metro_then_taxi and any(
            m in draft.preferences.excluded_modes for m in (Mode.metro, Mode.taxi)
        ):
            r.questions = ["先地铁再打车与禁止乘坐地铁或打车的条件冲突，请调整其中一项。"]
            return r
        r.total_stops, r.budget_cents, r.budget_scope = (
            len(draft.stops),
            draft.budget_cents,
            draft.budget_scope,
        )
        self.venues = {
            name for stop in draft.stops if stop.label in {"演唱会", "比赛"} for name in stop.locations
        }
        if not draft.origin or not draft.stops:
            r.questions = ["从哪里出发，要按顺序去哪些地方？"]
            return r
        start = draft.depart_after
        if start is None:
            anchor = next((s.start_at for s in draft.stops if s.start_at), None)
            if anchor:
                start = anchor - timedelta(hours=5)
                r.assumptions.append(f"未限制出发时间，先从活动前5小时（{start:%m-%d %H:%M}）搜索，可调整")
            else:
                anchored = next((s.label for s in draft.stops if s.requires_start_time), None)
                r.questions = [
                    f"{anchored}是哪一天、几点开始？也可以给出出发日期和时间。"
                    if anchored
                    else "哪一天出行，计划何时出发？"
                ]
                # Still make the restaurant selection work explicit while awaiting the anchor.
                for i, s in enumerate(draft.stops):
                    if s.label == "用餐":
                        await self.choices(draft, i)
                        break
                return r
        r.departure_at = start
        horizon = draft.arrive_by or start + timedelta(days=2)
        if draft.arrive_by is None:
            r.assumptions.append("未指定最终到达期限，先比较出发后48小时内的接续；这不是你的最晚到达要求")
        candidates = [JourneyOption(location=draft.origin, ready=start)]
        try:
            for index, stop in enumerate(draft.stops):
                locations = await self.choices(draft, index)
                if not locations:
                    r.data_gaps.append(f"未找到{stop.label}的可用地点，保留此前已计算的行程")
                    break
                minutes = stop.duration_min
                if minutes is None:
                    minutes = 90 if stop.label == "用餐" else (150 if stop.requires_start_time else 0)
                    r.assumptions.append(f"{stop.label}暂按{minutes}分钟测算，可修改；不代表实际活动时长")
                if stop.requires_start_time and stop.start_at is None:
                    r.questions.append(f"{stop.label}几点开场？当前只能计算到场路线，不能保证赶得上开场。")
                expanded = []
                for candidate in candidates:
                    for location in locations:
                        # Compare departing after dinner with the next morning, without silently allowing overnight.
                        departures = [candidate.ready]
                        if index == len(draft.stops) - 1 and index > 0 and draft.allow_overnight is not False:
                            morning = (candidate.ready + timedelta(days=1)).replace(
                                hour=6, minute=0, second=0, microsecond=0
                            )
                            if candidate.ready.hour < 6:
                                morning -= timedelta(days=1)
                            if morning < horizon:
                                departures.append(morning)
                        for depart in departures:
                            if draft.arrival_priority and len(draft.stops) == 1:
                                routes = await self.arrival_routes(draft)
                            else:
                                routes = await self.routes(candidate.location, location, depart)
                            if (
                                not draft.arrival_priority
                                and not self.metro_then_taxi
                                and (not routes or index == len(draft.stops) - 1)
                            ):
                                routes += await self.routes(candidate.location, location, depart, "8")
                            for route in routes:
                                if any(s.mode in draft.preferences.excluded_modes for s in route.steps):
                                    self.reject("包含禁用交通方式")
                                    continue
                                if any(
                                    s.mode == Mode.shared_bike
                                    and (
                                        draft.preferences.cycling_acceptance == 0
                                        or (draft.preferences.cycling_acceptance == 1 and s.distance_m > 3000)
                                    )
                                    for s in route.steps
                                ):
                                    self.reject("不符合骑行接受度")
                                    continue
                                if (
                                    sum(
                                        s.distance_m
                                        for x in [*candidate.routes, route]
                                        for s in x.steps
                                        if s.mode == Mode.walk
                                    )
                                    > draft.max_walk_m
                                ):
                                    self.reject("累计步行超过限制")
                                    continue
                                all_steps = [s for x in [*candidate.routes, route] for s in x.steps]
                                if (
                                    sum(s.distance_m for s in all_steps if s.mode == Mode.shared_bike)
                                    > draft.max_bike_m
                                ):
                                    self.reject("累计骑行超过限制")
                                    continue
                                rides = [s for s in all_steps if s.mode not in {Mode.walk, Mode.shared_bike}]
                                if (
                                    draft.max_transfers is not None
                                    and max(0, len(rides) - 1) > draft.max_transfers
                                ):
                                    self.reject("超过换乘次数硬上限")
                                    continue
                                if stop.start_at and route.arrival > stop.start_at:
                                    self.reject("到达晚于活动开场")
                                    continue
                                event_start = stop.start_at or route.arrival
                                finish = event_start + timedelta(minutes=minutes)
                                if finish > horizon:
                                    self.reject("超出最终到达期限")
                                    if index == len(draft.stops) - 1:
                                        self.tradeoff(route, candidate, "需要放宽最晚到达时间", draft)
                                    continue
                                if draft.allow_overnight is False and finish.date() != start.date():
                                    self.reject("违反不跨夜要求")
                                    continue
                                transport = candidate.transport_cents + (route.cost_cents or 0)
                                activity_cost = candidate.activity_cents + (stop.cost_cents or 0)
                                total = transport + (activity_cost if draft.budget_scope == "total" else 0)
                                if draft.budget_cents is not None and total > draft.budget_cents:
                                    self.reject("累计费用超过预算")
                                    r.min_over_budget_cents = min(total, r.min_over_budget_cents or total)
                                    if index == len(draft.stops) - 1:
                                        self.tradeoff(route, candidate, "需要提高预算", draft)
                                    continue
                                decisions = list(candidate.decisions)
                                if len(locations) > 1 or location not in stop.locations:
                                    decisions.append(
                                        f"{stop.label}候选选用{location}，计入接驳后与其他门店比较"
                                    )
                                if depart != candidate.ready:
                                    decisions.append(
                                        f"比较次日{depart:%m-%d %H:%M}出发；需要安排等候或住宿，费用未核实"
                                    )
                                if (
                                    draft.allow_overnight is None
                                    and finish.date() != start.date()
                                    and not self.metro_then_taxi
                                ):
                                    decisions.append("此候选需要跨夜，待确认是否接受")
                                expanded.append(
                                    JourneyOption(
                                        location=location,
                                        ready=finish,
                                        routes=[*candidate.routes, route],
                                        activities=[
                                            *candidate.activities,
                                            Activity(
                                                label=stop.label,
                                                location=location,
                                                start=event_start,
                                                end=finish,
                                                cost_cents=stop.cost_cents or 0,
                                            ),
                                        ],
                                        transport_cents=transport,
                                        activity_cents=activity_cost,
                                        unknown_cost=candidate.unknown_cost
                                        or route.cost_cents is None
                                        or route.cost_incomplete
                                        or (
                                            draft.budget_scope == "total"
                                            and minutes > 0
                                            and stop.cost_cents is None
                                        ),
                                        decisions=decisions,
                                    )
                                )
                if not expanded:
                    r.data_gaps.append(
                        f"前一站 → {' / '.join(locations)}：当前候选未通过全程约束；不能据此断言没有其他可行路线"
                    )
                    break
                candidates = self.shortlist(expanded)
                r.options, r.completed_stops = candidates, index + 1
        except (DeadlineExceeded, asyncio.TimeoutError):
            r.data_gaps.append("本轮查询时间已到，保留已完成的行程部分")
        if (
            not self.metro_then_taxi
            and len(draft.stops) > 1
            and Mode.flight not in draft.preferences.excluded_modes
        ):
            r.data_gaps.append("机票实时价格与余票接口尚未接入，尚未完成飞机与铁路的全量比较")
        return r

    def tradeoff(
        self, route: RouteEvidence, before: JourneyOption, reason: str, draft: ItineraryDraft
    ) -> None:
        """Explain a counterfactual without mixing constraint-violating routes into accepted options."""
        trains = [s for s in route.steps if s.scheduled]
        if not trains:
            return
        cost = None if route.cost_cents is None else before.transport_cents + route.cost_cents
        if draft.budget_scope == "total" and cost is not None:
            cost += before.activity_cents
        violations = [reason]
        if draft.budget_cents is not None and cost is not None and cost > draft.budget_cents:
            violations.append("需要提高预算")
        item = dict(
            reason="；".join(dict.fromkeys(violations)),
            cost_cents=cost,
            arrival=route.arrival.isoformat(),
            trains=[s.model_dump(mode="json") for s in trains],
            source=route.source,
            cost_incomplete=route.cost_incomplete or before.unknown_cost,
        )
        if item not in self.report.tradeoffs:
            self.report.tradeoffs.append(item)
            self.report.tradeoffs = sorted(
                self.report.tradeoffs,
                key=lambda x: (x["cost_cents"] is None, x["cost_cents"] or 0, x["arrival"]),
            )[:6]


def describe_report(r: PlanningReport) -> str:
    """Explain decisions and evidence; never claim partial candidates complete the journey."""
    parts = [f"本轮按 **{r.departure_at:%Y-%m-%d %H:%M}** 出发查询。"] if r.departure_at else []
    if r.arrival_priority and r.arrive_by:
        parts = [f"已理解为 **{r.arrive_by:%Y-%m-%d %H:%M}前到达**，已按到达期限倒推并重新查询出发候选。"]
    if r.metro_then_taxi and r.options:
        best = r.options[0]
        last_taxi = next(s for s in reversed(best.routes[-1].steps) if s.mode == Mode.taxi)
        parts.append(
            f"**推荐在{last_taxi.origin}下车，再打车到{last_taxi.destination}。** "
            "程序已检查候选站的地铁衔接和末班时间，并按你的排序偏好自动选站（默认优先地铁加打车总估价）。"
            if not best.unknown_cost
            else f"**可在{last_taxi.origin}下车再打车。** 费用资料不全，暂不能判断哪个下车站最省钱。"
        )
    elif r.metro_then_taxi:
        parts.append("已尝试自动搜索下车换打车的站点，本轮尚未查到满足末班衔接和费用条件的方案。")
    elif not r.options and r.total_stops == 1:
        parts.append("已按上述出发时间查询，本轮返回的路线未通过时间或交通方式校验。")
    elif not r.options:
        parts.append("我来选择接驳站点、比较顺路餐厅和后续出发时间，不需要你先把每个地点都定好。")
    elif r.completed_stops < r.total_stops:
        parts.append(
            f"先给你已算出的前{r.completed_stops}站安排；后续接续仍有数据或约束缺口，下面不是完整可执行方案。"
        )
    elif any(s.scheduled for o in r.options for route in o.routes for s in route.steps):
        parts.append(
            "已按活动顺序比较门店和接续时间，下面是全程候选。车次来自高德，需在售票方确认服务日期、票价和余票。"
        )
    else:
        parts.append("已查询高德并检查路线衔接，下面是本轮推荐路线；时间和费用为参考估算。")
    for index, option in enumerate(display_options(r)):
        cost = f"交通参考合计¥{option.transport_cents / 100:.2f}"
        if option.unknown_cost:
            cost += "（另有未知费用，不能确认满足总预算）"
        parts.append(f"**候选{index + 1}：{cost}**")
        for route, activity in zip(option.routes, option.activities):
            fee = "费用待核实" if route.cost_cents is None else f"参考¥{route.cost_cents / 100:.2f}"
            parts.append(
                f"**{route.origin} → {route.destination}**，{route.departure:%m-%d %H:%M}出发，预计{route.arrival:%m-%d %H:%M}到达，{fee}。"
            )
            for step in route.steps:
                if step.mode == Mode.walk:
                    parts.append(
                        f"- {step.name}，约{(step.arrival - step.departure).total_seconds() / 60:.0f}分钟"
                    )
                else:
                    qualifier = "列车时刻" if step.scheduled else "推算时刻"
                    parts.append(
                        f"- **{step.name}**：{step.origin} → {step.destination}，{step.departure:%m-%d %H:%M}—{step.arrival:%m-%d %H:%M}（{qualifier}）"
                    )
                    if step.last_boarding:
                        parts.append(
                            f"  该方向上车站末班：{step.last_boarding:%H:%M}；推算上车：{step.departure:%H:%M}。"
                        )
            if r.metro_then_taxi:
                parts.append("；".join(route.warnings))
            if activity.end > activity.start:
                parts.append(
                    f"{activity.label}：{activity.start:%m-%d %H:%M}—{activity.end:%m-%d %H:%M}，{activity.location}。"
                )
            parts.append(f"[路线来源：高德]({route.source})，查询于{route.queried_at:%m-%d %H:%M}。")
        if option.decisions:
            parts.append("选择说明：" + "；".join(dict.fromkeys(option.decisions)))
    if r.rejected:
        parts.append("**淘汰原因**：" + "；".join(f"{k}（{v}项）" for k, v in r.rejected.items()))
    if r.min_over_budget_cents is not None:
        parts.append(
            f"已查到但超预算的接续中，最低累计参考费用为¥{r.min_over_budget_cents / 100:.2f}；这不是全市场最低价。"
        )
    if r.tradeoffs:
        parts.append("**如果愿意调整条件，可以比较以下接续（尚未满足原约束）**：")
        for alternative in r.tradeoffs[:3]:
            names = " → ".join(
                f"{t['name']}（{t['origin']} {datetime.fromisoformat(t['departure']):%m-%d %H:%M} → "
                f"{t['destination']} {datetime.fromisoformat(t['arrival']):%m-%d %H:%M}）"
                for t in alternative["trains"]
            )
            fare = (
                "费用未知"
                if alternative["cost_cents"] is None
                else f"此前行程加此接续参考¥{alternative['cost_cents'] / 100:.2f}"
            )
            if alternative.get("cost_incomplete"):
                fare += "（含未核实的打车/其他支出，预算仍待校验）"
            parts.append(
                f"- {names}；{fare}；预计到目的地{datetime.fromisoformat(alternative['arrival']):%m-%d %H:%M}。{alternative['reason']}。"
            )
    if r.assumptions:
        parts.append("**本轮测算条件**：\n\n" + "\n".join(f"- {a}" for a in dict.fromkeys(r.assumptions)))
    if r.data_gaps:
        parts.append("**尚未解决**：\n\n" + "\n".join(f"- {a}" for a in dict.fromkeys(r.data_gaps)))
    if r.questions:
        parts.append("只需优先确认：" + "；".join(list(dict.fromkeys(r.questions))[:2]))
    if r.options and not r.metro_then_taxi:
        parts.append(
            "公共交通末班按返回的站点运营时间检查；当地交通耗时、场馆入场、餐厅营业/排队和车票库存仍需核实。费用默认不含餐饮、门票及住宿，除非已明确计入。"
        )
    return "\n\n".join(parts)


def display_options(report: PlanningReport) -> list[JourneyOption]:
    """Do not inflate similar city-bus variants into supposedly distinct whole-trip choices."""
    if report.metro_then_taxi:
        return report.options[:1]
    selected, seen = [], set()
    for option in report.options:
        key = (
            tuple(a.location for a in option.activities),
            tuple(
                (s.name, s.departure.isoformat())
                for route in option.routes
                for s in route.steps
                if s.scheduled
            ),
        )
        if key not in seen:
            selected.append(option)
            seen.add(key)
        if len(selected) == 2:
            break
    return selected
