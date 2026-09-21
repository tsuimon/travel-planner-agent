"""Real-routing interpretation and whole-trip decision contracts without network access."""

from datetime import timedelta
from time import monotonic
from pydantic import SecretStr

from src.agent.intelligent import IntelligentPlanner, describe_report
from src.domain import ChatRequest, ItineraryDraft, ItineraryStop, Mode, Preferences
from src.search.evidence import RouteEvidence, RouteStep, normalize_transit


def railway(clock="1100", arrival="1200"):
    return {
        "railway": {
            "trip": "G123",
            "time": "3600",
            "departure_stop": {"name": "甲站", "time": clock},
            "arrival_stop": {"name": "乙站", "time": arrival},
        }
    }


def response(segments, duration="3600", fee="50"):
    return {
        "status": "1",
        "route": {"transits": [{"cost": {"duration": duration, "transit_fee": fee}, "segments": segments}]},
    }


def test_actual_train_clock_overrides_short_aggregate_duration(now):
    routes, errors = normalize_transit(response([railway("2041", "2141")], "120"), "甲", "乙", now)
    assert not errors and routes[0].arrival.hour >= 21
    assert routes[0].steps[0].departure.hour == 20
    assert routes[0].cost_cents == 5000


def test_missed_train_is_not_moved_to_tomorrow(now):
    routes, errors = normalize_transit(response([railway("0900", "1000")]), "甲", "乙", now)
    assert routes == [] and errors == ["rail_connection_too_short"]


def test_early_bus_query_waits_for_first_service(now):
    bus = {
        "bus": {
            "buslines": [
                {
                    "type": "地铁线路",
                    "name": "1号线",
                    "departure_stop": {"name": "甲"},
                    "arrival_stop": {"name": "乙"},
                    "station_start_time": "0600",
                    "station_end_time": "2300",
                    "cost": {"duration": "600"},
                }
            ]
        }
    }
    routes, errors = normalize_transit(response([bus], "600"), "甲", "乙", now.replace(hour=5))
    assert not errors and routes[0].steps[0].departure.hour == 6
    closed, _ = normalize_transit(response([bus]), "甲", "乙", now.replace(hour=23, minute=30))
    assert not closed


def test_cross_midnight_train_uses_duration_and_arrival_clock(now):
    segment = railway("2300", "0100")
    segment["railway"]["time"] = "7200"
    routes, errors = normalize_transit(response([segment]), "甲", "乙", now)
    assert not errors and routes[0].steps[0].arrival.date() == (now + timedelta(days=1)).date()


def quote(a, b, at, minutes=30, cost=100):
    end = at + timedelta(minutes=minutes)
    return RouteEvidence(
        origin=a,
        destination=b,
        departure=at,
        arrival=end,
        cost_cents=cost,
        queried_at=at,
        steps=[
            RouteStep(
                mode=Mode.normal_rail,
                name="TEST-TRAIN",
                origin=a,
                destination=b,
                departure=at,
                arrival=end,
                scheduled=True,
            )
        ],
    )


async def test_choose_restaurant_by_whole_trip_cost_not_first_leg(service, now, monkeypatch):
    planner = IntelligentPlanner(service.registry, monotonic() + 10)
    trip = ItineraryDraft(
        origin="A",
        depart_after=now,
        arrive_by=now + timedelta(hours=10),
        allow_overnight=False,
        stops=[
            ItineraryStop(label="用餐", locations=["餐厅"], duration_min=60),
            ItineraryStop(locations=["C"], duration_min=0),
        ],
    )

    async def choices(draft, index):
        return ["B1", "B2"] if index == 0 else ["C"]

    async def routes(a, b, at, strategy="1"):
        # B1 is cheap to reach but expensive to leave. A greedy restaurant choice is wrong.
        price = {("A", "B1"): 100, ("A", "B2"): 200, ("B1", "C"): 1000, ("B2", "C"): 100}[(a, b)]
        return [quote(a, b, at, cost=price)]

    monkeypatch.setattr(planner, "choices", choices)
    monkeypatch.setattr(planner, "routes", routes)
    report = await planner.plan(trip)
    assert report.completed_stops == 2
    assert report.options[0].activities[0].location == "B2"
    assert report.options[0].transport_cents == 300
    assert report.options[0].routes[1].departure >= report.options[0].activities[0].end


async def test_budget_violation_stays_out_of_accepted_candidates(service, now, monkeypatch):
    planner = IntelligentPlanner(service.registry, monotonic() + 10)
    trip = ItineraryDraft(
        origin="A",
        depart_after=now,
        arrive_by=now + timedelta(hours=10),
        budget_cents=500,
        allow_overnight=False,
        stops=[ItineraryStop(locations=["C"], duration_min=0)],
    )

    async def routes(a, b, at, strategy="1"):
        return [quote(a, b, at, cost=1000)]

    monkeypatch.setattr(planner, "routes", routes)
    report = await planner.plan(trip)
    assert not report.options and report.tradeoffs[0]["cost_cents"] == 1000
    assert "需要提高预算" in describe_report(report)


async def test_data_failure_preserves_completed_prefix(service, now, monkeypatch):
    planner = IntelligentPlanner(service.registry, monotonic() + 10)
    trip = ItineraryDraft(
        origin="A",
        depart_after=now,
        arrive_by=now + timedelta(hours=10),
        allow_overnight=False,
        stops=[
            ItineraryStop(locations=["B"], duration_min=0),
            ItineraryStop(locations=["C"], duration_min=0),
        ],
    )

    async def routes(a, b, at, strategy="1"):
        return [quote(a, b, at)] if b == "B" else []

    monkeypatch.setattr(planner, "routes", routes)
    report = await planner.plan(trip)
    assert report.completed_stops == 1 and report.options
    assert "不是完整可执行方案" in describe_report(report)


async def test_request_for_train_does_not_reask_restaurant(service, now, monkeypatch):
    first = await service.chat(
        ChatRequest(message="我在北京大兴清源路，去天津奥体看演唱会，然后吃海底捞，然后去广州"), now
    )
    service.settings.amap_api_key = SecretStr("test")
    called = []

    async def plan(self, draft):
        called.append(draft)
        self.report.total_stops = 3
        self.report.questions = ["演出是哪一天、几点开场？"]
        return self.report

    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    result = await service.chat(ChatRequest(message="我需要具体车次", session_id=first.session_id), now)
    assert called and "具体地点或可接受" not in result.answer
    assert "我来选择接驳站点" in result.answer


async def test_no_overnight_is_a_hard_constraint(service, now, monkeypatch):
    planner = IntelligentPlanner(service.registry, monotonic() + 10)
    at = now.replace(hour=23)
    trip = ItineraryDraft(
        origin="A",
        depart_after=at,
        arrive_by=at + timedelta(hours=5),
        allow_overnight=False,
        preferences=Preferences(excluded_modes=[Mode.flight]),
        stops=[ItineraryStop(locations=["B"], duration_min=0)],
    )

    async def routes(a, b, at, strategy="1"):
        return [quote(a, b, at, minutes=120)]

    monkeypatch.setattr(planner, "routes", routes)
    report = await planner.plan(trip)
    assert not report.options and report.rejected["违反不跨夜要求"] > 0


def test_explicit_clear_deadline_survives_llm_history_copy(now):
    from src.agent.itinerary_parser import rule_draft, preserve_known_fields

    old = ItineraryDraft(origin="A", depart_after=now, arrive_by=now + timedelta(hours=5), budget_cents=500)
    rule = rule_draft("取消最晚到达时间限制，预算不限", old.preferences, old)
    merged = preserve_known_fields(old.model_copy(deep=True), rule, old)
    assert merged.arrive_by is None and merged.budget_cents is None


def test_unknown_taxi_cost_is_not_presented_as_complete_quote(now):
    taxi = {"taxi": {"drivetime": "600", "distance": "2000", "price": ""}}
    routes, _ = normalize_transit(response([taxi, railway()]), "甲", "乙", now)
    assert routes and routes[0].cost_incomplete
    assert routes[0].steps[0].mode == Mode.taxi


def test_natural_chinese_example_keeps_all_stops_and_distinct_event_times():
    from src.agent.itinerary_parser import rule_draft

    value = rule_draft(
        "2026年9月26日15点从北京大兴清源路出发，19点半在天津奥体看演唱会，预计22点散场，"
        "吃一小时海底捞后去广州。交通预算500元，允许过夜，餐厅帮我选。",
        Preferences(),
    )
    assert value.origin == "北京大兴清源路"
    assert value.depart_after.hour == 15
    assert [s.locations for s in value.stops] == [["天津奥体"], ["海底捞"], ["广州"]]
    assert value.stops[0].start_at.hour == 19 and value.stops[0].start_at.minute == 30
    assert value.stops[0].duration_min == 150 and value.stops[1].duration_min == 60
    assert value.arrive_by is None


def test_flexible_meal_cannot_be_pinned_to_concert_finish(now):
    from src.agent.itinerary_parser import preserve_known_fields

    known = ItineraryDraft(stops=[ItineraryStop(label="用餐", locations=["海底捞"], duration_min=60)])
    predicted = known.model_copy(deep=True)
    predicted.stops[0].start_at = now
    assert preserve_known_fields(predicted, known, None).stops[0].start_at is None


def test_combined_taxi_train_segment_uses_station_endpoints_for_order(now):
    segment = railway()
    segment["taxi"] = {"drivetime": "600", "distance": "2000", "endname": "甲站"}
    routes, errors = normalize_transit(response([segment]), "餐厅", "乙站", now)
    assert not errors and [s.mode for s in routes[0].steps] == [Mode.taxi, Mode.high_speed_rail]
    assert routes[0].steps[0].arrival < routes[0].steps[1].departure
