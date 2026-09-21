"""Ordered activity planning with candidate propagation instead of greedy leg selection."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256

from src.domain import Activity, Constraints, ItineraryDraft, Leg, Mode, Network, Plan, Weather
from src.risk import annotate
from src.search.algorithm import SearchStats, transfers
from src.search.planner import HierarchicalPlanner


@dataclass
class Candidate:
    location: str
    ready: datetime
    legs: list[Leg] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    cost: int = 0
    risks: list[str] = field(default_factory=list)


class ItineraryPlanner:
    """Use the same validated tools and OD solver at each visited location/time."""

    def __init__(self, registry, settings) -> None:
        self.registry, self.settings = registry, settings
        self.errors: list[str] = []

    async def plan(self, draft: ItineraryDraft, stats: SearchStats) -> list[Plan]:
        assert draft.origin and draft.depart_after and draft.arrive_by
        candidates = [Candidate(draft.origin, draft.depart_after)]
        for index, stop in enumerate(draft.stops):
            expanded = []
            for candidate in candidates:
                for location in stop.locations:
                    deadline = min(stop.start_at or draft.arrive_by, draft.arrive_by)
                    same_place = candidate.location == location
                    if candidate.ready > deadline or (candidate.ready == deadline and not same_place):
                        continue
                    # Each route query has a bounded horizon; the overall tour can span seven days.
                    deadline = min(deadline, candidate.ready + timedelta(hours=48))
                    until = (
                        min(
                            draft.depart_before or draft.depart_after + timedelta(minutes=15),
                            deadline - timedelta(seconds=1),
                        )
                        if index == 0
                        else deadline - timedelta(seconds=1)
                    )
                    if until < candidate.ready and not same_place:
                        continue
                    remaining_budget = (
                        None if draft.budget_cents is None else draft.budget_cents - candidate.cost
                    )
                    if remaining_budget is not None and remaining_budget < 0:
                        continue
                    if same_place:
                        paths = [Plan(id="stay", legs=[], total_cost_cents=0, total_minutes=0, transfers=0)]
                    else:
                        constraints = Constraints(
                            **draft.preferences.model_dump(),
                            origin=candidate.location,
                            destination=location,
                            depart_after=candidate.ready,
                            depart_before=until,
                            arrive_by=deadline,
                            budget_cents=remaining_budget,
                            max_walk_m=draft.max_walk_m,
                            max_bike_m=draft.max_bike_m,
                            max_transfers=draft.max_transfers,
                        )
                        paths = await self.routes(constraints, stats)
                    for route in paths:
                        arrived = route.legs[-1].arrival if route.legs else candidate.ready
                        start = stop.start_at or arrived
                        if arrived > start:
                            continue
                        finish = start + timedelta(minutes=stop.duration_min or 0)
                        if finish > draft.arrive_by:
                            continue
                        if not draft.allow_overnight and finish.date() != draft.depart_after.date():
                            continue
                        cost = candidate.cost + route.total_cost_cents
                        if draft.budget_scope == "total":
                            cost += stop.cost_cents or 0
                        if draft.budget_cents is not None and cost > draft.budget_cents:
                            continue
                        legs = candidate.legs + route.legs
                        if sum(x.distance_m for x in legs if x.mode == Mode.walk) > draft.max_walk_m:
                            continue
                        if sum(x.distance_m for x in legs if x.mode == Mode.shared_bike) > draft.max_bike_m:
                            continue
                        if draft.max_transfers is not None and transfers(legs) > draft.max_transfers:
                            continue
                        activities = candidate.activities + [
                            Activity(
                                label=stop.label,
                                location=location,
                                start=start,
                                end=finish,
                                cost_cents=stop.cost_cents or 0,
                            )
                        ]
                        ready = max(finish, arrived + timedelta(minutes=self.settings.transfer_buffer_min))
                        expanded.append(
                            Candidate(location, ready, legs, activities, cost, candidate.risks + route.risks)
                        )
            if not expanded:
                return []
            # Preserve both cheap and early labels for downstream connections, plus location alternatives.
            unique = {
                (c.location, c.ready.isoformat(), tuple(x.edge_id for x in c.legs)): c for c in expanded
            }
            ordered = sorted(unique.values(), key=lambda c: (c.cost, c.ready))
            early = sorted(unique.values(), key=lambda c: (c.ready, c.cost))
            candidates = []
            for cheap, fast in zip(ordered, early):
                for value in (cheap, fast):
                    if value not in candidates:
                        candidates.append(value)
                if len(candidates) >= 12:
                    break
            if len(candidates) < len(unique):
                stats.truncated = True
        plans = []
        for candidate in candidates:
            finish = candidate.activities[-1].end
            start = candidate.legs[0].departure if candidate.legs else draft.depart_after
            identity = repr([(x.edge_id, x.departure.isoformat()) for x in candidate.legs]) + repr(
                candidate.activities
            )
            risks = candidate.risks + [
                "多站候选搜索受数量限制，不保证全局最优",
                "活动时间由用户提供，餐厅营业和演出实际散场尚需确认",
                "费用仅包含交通，未计餐饮、门票和住宿"
                if draft.budget_scope == "transport"
                else "活动费用采用用户提供金额；未知实际费用仍需核实",
            ]
            plans.append(
                Plan(
                    id=sha256(identity.encode()).hexdigest()[:16],
                    legs=candidate.legs,
                    activities=candidate.activities,
                    total_cost_cents=candidate.cost,
                    total_minutes=(finish - start).total_seconds() / 60,
                    transfers=transfers(candidate.legs),
                    risks=list(dict.fromkeys(risks)),
                )
            )
        return plans

    async def routes(self, c: Constraints, stats: SearchStats) -> list[Plan]:
        positions = await asyncio.gather(
            *[
                self.registry.call(
                    "geocode", {"address": address, "at": c.depart_after.isoformat()}, stats.deadline
                )
                for address in (c.origin, c.destination)
            ]
        )
        if not all(r.success for r in positions):
            self.errors.append("itinerary_geocode_unavailable")
            return []
        c = Constraints.model_validate(
            {**c.model_dump(), "origin": positions[0].data["id"], "destination": positions[1].data["id"]}
        )
        result = await self.registry.call(
            "transit_query",
            {
                "origin": c.origin,
                "destination": c.destination,
                "depart_after": c.depart_after.isoformat(),
                "arrive_by": c.arrive_by.isoformat(),
            },
            stats.deadline,
        )
        if not result.success:
            self.errors.append("itinerary_transit_unavailable")
            return []
        network = Network.model_validate(result.data)
        planner = HierarchicalPlanner(self.settings.transfer_buffer_min)
        plans = []
        for objective in ("cost", "time", "transfers"):
            plans += await asyncio.to_thread(planner.plan, network, c, objective, stats)
        return [annotate(plan, Weather(), result.cached) for plan in plans]
