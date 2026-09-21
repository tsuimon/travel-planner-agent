"""Ordered activity scheduling, cumulative budgets, and downstream connection regression."""

from datetime import timedelta

from src.agent.itinerary_parser import missing_fields, preserve_known_fields, rule_draft
from src.domain import ChatRequest, ItineraryDraft, ItineraryStop, Leg, Mode, Plan, Preferences
from src.search.algorithm import SearchStats
from src.search.itinerary import ItineraryPlanner


def draft(now, **changes):
    values = dict(
        origin="A",
        depart_after=now,
        arrive_by=now + timedelta(hours=10),
        allow_overnight=True,
        stops=[
            ItineraryStop(label="用餐", locations=["B"], duration_min=60),
            ItineraryStop(locations=["C"], duration_min=0),
        ],
    )
    return ItineraryDraft(**{**values, **changes})


def path(a, b, start, minutes, cost):
    leg = Leg(
        edge_id=f"{a}-{b}-{minutes}",
        origin=a,
        destination=b,
        origin_name=a,
        destination_name=b,
        mode=Mode.bus,
        service_id=f"{a}-{b}",
        departure=start,
        arrival=start + timedelta(minutes=minutes),
        cost_cents=cost,
        distance_m=0,
        source="synthetic://test",
        observed_at=start,
        demo=True,
        official=False,
    )
    return Plan(id=leg.edge_id, legs=[leg], total_cost_cents=cost, total_minutes=minutes, transfers=0)


async def test_early_costly_label_survives_to_catch_onward_service(service, now, monkeypatch):
    planner = ItineraryPlanner(service.registry, service.settings)
    connection = now + timedelta(hours=3)

    async def routes(c, stats):
        if c.origin == "A":
            return [path("A", "B", now, 180, 100), path("A", "B", now, 60, 500)]
        return [path("B", "C", connection, 60, 100)] if c.depart_after <= connection else []

    monkeypatch.setattr(planner, "routes", routes)
    plans = await planner.plan(draft(now), SearchStats())
    assert len(plans) == 1 and plans[0].total_cost_cents == 600
    assert plans[0].activities[0].end == now + timedelta(hours=2)
    assert [a.location for a in plans[0].activities] == ["B", "C"]


async def test_total_budget_includes_activity_cost(service, now, monkeypatch):
    planner = ItineraryPlanner(service.registry, service.settings)

    async def routes(c, stats):
        return [path(c.origin, c.destination, c.depart_after, 30, 200)]

    monkeypatch.setattr(planner, "routes", routes)
    trip = draft(now, budget_cents=1000, budget_scope="total")
    trip.stops[0].cost_cents = 700
    assert not await planner.plan(trip, SearchStats())
    trip.budget_scope = "transport"
    assert (await planner.plan(trip, SearchStats()))[0].total_cost_cents == 400


async def test_missed_event_and_overnight_are_filtered(service, now, monkeypatch):
    planner = ItineraryPlanner(service.registry, service.settings)

    async def routes(c, stats):
        return [path(c.origin, c.destination, c.depart_after, 90, 200)]

    monkeypatch.setattr(planner, "routes", routes)
    trip = draft(now)
    trip.stops[0].start_at = now + timedelta(minutes=60)
    assert not await planner.plan(trip, SearchStats())
    trip.stops[0].start_at = None
    trip.depart_after = now.replace(hour=23)
    trip.arrive_by = trip.depart_after + timedelta(hours=10)
    trip.allow_overnight = False
    assert not await planner.plan(trip, SearchStats())


async def test_same_place_activity_needs_no_route_query(service, now, monkeypatch):
    planner = ItineraryPlanner(service.registry, service.settings)

    async def unexpected(*args):
        raise AssertionError("Staying in place should not query transport")

    monkeypatch.setattr(planner, "routes", unexpected)
    trip = draft(now, stops=[ItineraryStop(label="会议", locations=["A"], start_at=now, duration_min=30)])
    plans = await planner.plan(trip, SearchStats())
    assert len(plans) == 1 and plans[0].total_minutes == 30 and not plans[0].legs


async def test_structured_multistop_end_to_end(service, now):
    trip = draft(
        now.replace(hour=18),
        origin="haidian",
        arrive_by=now.replace(hour=23),
        preferences=Preferences(excluded_modes=[Mode.taxi]),
        stops=[
            ItineraryStop(label="用餐", locations=["beijing"], duration_min=30),
            ItineraryStop(locations=["binhai"], duration_min=0),
        ],
    )
    result = await service.chat(ChatRequest(message="按这些地点规划", itinerary=trip), now)
    assert result.status == "ok" and result.plans
    for plan in result.plans:
        assert len(plan.activities) == 2
        assert all(leg.mode != Mode.taxi for leg in plan.legs)
        assert plan.activities[-1].end <= trip.arrive_by
        assert plan.total_cost_cents == sum(leg.cost_cents for leg in plan.legs)
        onward = next(leg for leg in plan.legs if leg.origin == "beijing")
        assert onward.departure >= plan.activities[0].end


def test_clarification_updates_keep_order_and_known_fields():
    first = rule_draft(
        "我在北京大兴清源路，26号晚上去天津奥体看演唱会，然后吃海底捞，然后去广州", Preferences()
    )
    second = rule_draft(
        "出发2026-09-26 15:00，最晚2026-09-27 22:00，演唱会2026-09-26 19:30，"
        "演唱会150分钟，用餐60分钟，用餐地点海底捞天津测试店，允许过夜，交通预算500",
        first.preferences,
        first,
    )
    assert not missing_fields(second)
    assert second.budget_cents == 50000 and second.origin == first.origin
    assert [s.label for s in second.stops] == ["演唱会", "用餐", "到达"]
    assert second.stops[-1].locations == ["广州"]


async def test_amap_adapter_validates_caches_and_omits_key(service, monkeypatch):
    import httpx
    from pydantic import SecretStr
    from time import monotonic

    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(
            200,
            json={
                "status": "1",
                "pois": [
                    {
                        "id": "test",
                        "name": "测试店",
                        "address": [],
                        "cityname": "天津市",
                        "location": "117.2,39.1",
                    }
                ],
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    service.settings.amap_api_key = SecretStr("test-key")
    args = {"keywords": "海底捞", "city": "天津"}
    first = await service.registry.call("amap_places", args, monotonic() + 10)
    second = await service.registry.call("amap_places", args, monotonic() + 10)
    assert first.success and second.cached and len(attempts) == 1
    assert first.data["places"][0]["address"] == ""
    assert "test-key" not in first.model_dump_json()


def test_model_cannot_drop_explicit_deadline_budget_or_activity_duration(now):
    known = draft(now, budget_cents=10000)
    predicted = known.model_copy(deep=True)
    predicted.arrive_by = None
    predicted.budget_cents = 999999
    predicted.stops[0].duration_min = None
    merged = preserve_known_fields(predicted, known, None)
    assert merged.arrive_by == known.arrive_by
    assert merged.budget_cents == 10000
    assert merged.stops[0].duration_min == 60


def test_flexible_model_update_can_change_previous_field(now):
    previous = draft(now)
    known = previous.model_copy(deep=True)
    predicted = previous.model_copy(deep=True)
    predicted.arrive_by += timedelta(hours=1)
    assert preserve_known_fields(predicted, known, previous).arrive_by == predicted.arrive_by
