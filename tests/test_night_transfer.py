"""Automatic alighting decisions, with unreachable cheap stations and live dispatch regressions."""

from datetime import timedelta
from time import monotonic
from pydantic import SecretStr
import pytest

from src.agent.intelligent import IntelligentPlanner, describe_report
from src.agent.parser import parse
from src.domain import ChatRequest, ItineraryDraft, ItineraryStop, Mode, Preferences
from src.search.night_transfer import taxi_route, usable_metro
from src.search.evidence import normalize_transit

QUERY = "10点半从鸟巢出发，去前往北京印刷学院，四号线赶不上了，先坐地铁，然后打车，给我花费最低的方案"


def bus_response(destination="中间站", end="2330", fee="5", start="始发站", station_id="route-stop"):
    return {
        "status": "1",
        "route": {
            "transits": [
                {
                    "cost": {"duration": "1200", "transit_fee": fee},
                    "segments": [
                        {
                            "bus": {
                                "buslines": [
                                    {
                                        "id": "L1",
                                        "type": "地铁线路",
                                        "name": "地铁测试线",
                                        "station_start_time": "0500",
                                        "station_end_time": end,
                                        "departure_stop": {
                                            "name": start,
                                            "id": station_id,
                                            "location": "116.0,39.0",
                                        },
                                        "arrival_stop": {"name": destination, "location": "116.1,39.0"},
                                        "cost": {"duration": "1200"},
                                    }
                                ]
                            }
                        }
                    ],
                }
            ]
        },
    }


def drive(fee="20"):
    return {
        "status": "1",
        "route": {
            "taxi_cost": fee,
            "paths": [
                {"distance": "5000", "cost": {"duration": "600"}},
            ],
        },
    }


def test_original_message_parses_half_hour_and_night_context(now):
    c = parse(QUERY, Preferences(), now)
    assert (c.origin, c.destination) == ("鸟巢", "北京印刷学院")
    assert (c.depart_after.hour, c.depart_after.minute) == (22, 30)
    explicit = parse("上午10点半从鸟巢出发，去北京印刷学院，赶不上了", Preferences(), now)
    assert explicit.depart_after.hour == 10


async def test_single_trip_uses_live_planner_and_replaces_old_multi_trip(service, now, monkeypatch):
    old = await service.chat(
        ChatRequest(message="我在北京大兴清源路，去天津奥体看演唱会，然后吃海底捞，然后去广州"), now
    )
    service.settings.amap_api_key = SecretStr("test")
    captured = []

    async def plan(self, draft):
        captured.append(draft)
        return self.report

    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    result = await service.chat(ChatRequest(message=QUERY, session_id=old.session_id), now)
    draft = captured[-1]
    assert draft.metro_then_taxi and draft.origin == "鸟巢"
    assert len(draft.stops) == 1 and draft.stops[0].locations == ["北京印刷学院"]
    assert draft.preferences.budget_preference == "economy"
    assert "演示可使用" not in result.answer
    follow = await service.chat(ChatRequest(message="预算50元", session_id=old.session_id), now)
    assert captured[-1].metro_then_taxi and captured[-1].budget_cents == 5000
    assert follow.constraints.origin == "鸟巢"


async def test_known_landmark_alias_does_not_select_same_name_shop(service, monkeypatch):
    planner = IntelligentPlanner(service.registry, monotonic() + 5)
    addresses = []

    async def call(name, args):
        addresses.append(args["address"])
        return {
            "locations": [{"location": "116.1,39.0", "citycode": "010", "formatted_address": "国家体育场"}]
        }

    monkeypatch.setattr(planner, "call", call)
    await planner.resolve("鸟巢")
    assert addresses == ["北京国家体育场"]


async def test_select_station_by_full_cost_not_nearest_or_longest_ride(service, now, monkeypatch):
    planner = IntelligentPlanner(service.registry, monotonic() + 10)
    at = now.replace(hour=22, minute=30)
    planner.positions = {
        "出发地": {"location": "116.0,39.0", "citycode": "010"},
        "学校": {"location": "116.5,39.0", "citycode": "010"},
    }
    queried_stations = []

    async def call(name, args):
        if name == "amap_line":
            # IDs differ between the route and line-list APIs. Names and coordinates agree.
            return {
                "lines": [
                    {
                        "status": "1",
                        "busstops": [
                            {"id": "BV-origin", "name": "始发站", "location": "116.0,39.0"},
                            {"id": "BV-closed", "name": "最近但已停运", "location": "116.49,39.0"},
                            {"id": "BV-good", "name": "更远但总价低", "location": "116.3,39.0"},
                        ],
                    }
                ]
            }
        if args.get("mode") == "drive":
            return drive("10" if args["origin"] == "116.3,39.0" else "40")
        destination = args["destination"]
        if destination == "116.5,39.0":
            return bus_response()
        queried_stations.append(destination)
        if destination == "116.49,39.0":
            return bus_response("最近但已停运", "2200", "1")
        if destination == "116.3,39.0":
            return bus_response("更远但总价低", fee="6")
        return bus_response()

    monkeypatch.setattr(planner, "call", call)
    report = await planner.plan(
        ItineraryDraft(
            origin="出发地",
            depart_after=at,
            arrive_by=at + timedelta(hours=4),
            metro_then_taxi=True,
            budget_cents=2000,
            stops=[ItineraryStop(locations=["学校"], duration_min=0)],
        )
    )
    assert "116.3,39.0" in queried_stations and "116.49,39.0" in queried_stations
    assert report.completed_stops == 1
    assert report.options[0].transport_cents == 1600
    steps = report.options[0].routes[0].steps
    assert steps[-1].origin == "更远但总价低" and steps[-1].mode == Mode.taxi
    assert steps[-1].departure >= steps[-2].arrival + timedelta(minutes=10)
    assert "推荐在更远但总价低下车" in describe_report(report)
    assert all(o.transport_cents <= 2000 for o in report.options)


@pytest.mark.parametrize("end", ["", "2236"])
def test_unknown_or_too_tight_last_train_is_not_a_valid_night_prefix(now, end):
    at = now.replace(hour=22, minute=30)
    routes, _ = normalize_transit(bus_response(end=end), "A", "B", at)
    assert routes and not usable_metro(routes[0])


def test_after_midnight_service_end_uses_next_day(now):
    at = now.replace(hour=23, minute=30)
    routes, _ = normalize_transit(bus_response(end="0030"), "A", "B", at)
    assert usable_metro(routes[0])
    assert routes[0].steps[0].last_boarding.date() == (at + timedelta(days=1)).date()


def test_next_morning_waiting_is_not_a_night_connection(now):
    at = now.replace(hour=0, minute=30)
    routes, _ = normalize_transit(bus_response(end="2330"), "A", "B", at)
    assert routes and not usable_metro(routes[0])


def test_missing_taxi_quote_is_unknown_not_free(now):
    assert taxi_route(drive(""), "A", "B", now).cost_cents is None
    assert taxi_route(drive("23.50"), "A", "B", now).cost_cents == 2350


async def test_mixed_route_never_ignores_forbidden_mode(service, now):
    planner = IntelligentPlanner(service.registry, monotonic() + 5)
    report = await planner.plan(
        ItineraryDraft(
            origin="A",
            depart_after=now,
            metro_then_taxi=True,
            stops=[ItineraryStop(locations=["B"])],
            preferences=Preferences(excluded_modes=[Mode.taxi]),
        )
    )
    assert not report.options and report.questions and report.calls == 0


async def test_past_single_live_trip_does_not_query_old_date(service, now):
    service.settings.amap_api_key = SecretStr("test")
    result = await service.chat(ChatRequest(message="今天上午8点从北京到天津"), now)
    assert result.status == "clarification" and "已经过去" in result.answer
