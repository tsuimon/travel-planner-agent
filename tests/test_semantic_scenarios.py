"""Scenario contracts spanning semantic roles, follow-ups, hard limits and search decisions."""

from datetime import timedelta
from time import monotonic

import pytest
from pydantic import SecretStr

from src.agent.intelligent import IntelligentPlanner, describe_report
from src.agent.itinerary_parser import preserve_known_fields, rule_draft
from src.agent.llm import ParsedIntent
from src.agent.parser import parse
from src.domain import ChatRequest, ItineraryDraft, ItineraryStop, Mode, Preferences
from src.errors import AmbiguousArrivalTime
from src.search.evidence import RouteEvidence, RouteStep


@pytest.mark.parametrize(
    "clause",
    [
        "下午4点到",
        "下午四点到达",
        "16:00抵达",
        "最晚下午4点到",
        "到达时间为下午4点到达",
        "到达时间是下午4点",
        "最晚16:00",
        "不是下午4点出发，是下午4点到",
    ],
)
def test_arrival_clock_is_deadline_not_departure(now, clause):
    value = parse(f"今天从北京到天津，{clause}，预算100元，不要打车", Preferences(), now)
    assert value.arrive_by == now.replace(hour=16)
    assert value.depart_after < value.arrive_by and value.arrival_priority
    assert value.budget_cents == 10000


def test_distinct_departure_and_arrival_clocks(now):
    c = parse("今天下午3点从北京到天津，4点到", Preferences(), now)
    assert c.depart_after.hour == 15 and c.arrive_by.hour == 16 and not c.arrival_priority


def test_reverse_search_respects_departure_date_and_next_day_deadline(now):
    c = parse("明天从北京到天津，下午4点到", Preferences(), now)
    assert c.depart_after.date() == (now + timedelta(days=1)).date()
    c = parse("明天23点从北京到天津，次日01:00到", Preferences(), now)
    assert c.depart_after.hour == 23
    assert c.arrive_by == now.replace(hour=1) + timedelta(days=2)


def test_ambiguous_four_is_not_silently_am_or_pm(now):
    with pytest.raises(AmbiguousArrivalTime):
        parse("从北京到天津，4点到", Preferences(), now)


async def test_short_clarification_resumes_original_request_and_constraints(service, now, monkeypatch):
    service.settings.amap_api_key = SecretStr("test")
    drafts = []

    async def plan(self, draft):
        drafts.append(draft)
        return self.report

    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    first = await service.chat(ChatRequest(message="从北京到天津，4点到，预算100元，不要打车"), now)
    assert first.status == "clarification" and not drafts
    await service.chat(ChatRequest(message="下午", session_id=first.session_id), now)
    assert drafts[-1].arrive_by.hour == 16 and drafts[-1].origin == "北京"
    assert drafts[-1].preferences.excluded_modes == [Mode.taxi]
    assert drafts[-1].budget_cents == 10000
    await service.chat(ChatRequest(message="预算80元", session_id=first.session_id), now)
    assert drafts[-1].arrive_by.hour == 16 and drafts[-1].budget_cents == 8000
    assert drafts[-1].arrival_priority


@pytest.mark.parametrize("null_departure", [False, True])
async def test_model_cannot_change_arrival_to_departure_when_route_needs_llm(
    service, now, monkeypatch, null_departure
):
    service.settings.amap_api_key = SecretStr("test")
    service.settings.llm_api_key = SecretStr("test")
    service.settings.llm_base_url = "https://example.invalid"
    service.settings.llm_model = "test"
    values = []

    async def model(*args):
        return ParsedIntent(
            origin="北京",
            destination="天津",
            depart_after=None if null_departure else now.replace(hour=16).isoformat(),
            depart_before=None if null_departure else now.replace(hour=16).isoformat(),
            arrive_by=now.replace(hour=23).isoformat(),
        )

    async def plan(self, draft):
        values.append(draft)
        return self.report

    monkeypatch.setattr(service.llm, "parse", model)
    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    await service.chat(ChatRequest(message="北京去天津，下午4点到，步行不超过1公里，最多1次换乘"), now)
    assert values and values[0].arrive_by.hour == 16 and values[0].depart_after.hour == 10
    assert values[0].max_walk_m == 1000 and values[0].max_transfers == 1


async def test_model_understood_constraints_are_not_erased_by_rule_defaults(service, now, monkeypatch):
    service.settings.amap_api_key = SecretStr("test")
    service.settings.llm_api_key = SecretStr("test")
    service.settings.llm_base_url = "https://example.invalid"
    service.settings.llm_model = "test"
    drafts = []

    async def model(*args):
        return ParsedIntent(
            origin="北京",
            destination="天津",
            depart_after=now.replace(hour=15).isoformat(),
            depart_before=now.replace(hour=15).isoformat(),
            arrive_by=now.replace(hour=23).isoformat(),
            max_walk_m=500,
            excluded_modes=["taxi"],
        )

    async def plan(self, draft):
        drafts.append(draft)
        return self.report

    monkeypatch.setattr(service.llm, "parse", model)
    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    await service.chat(ChatRequest(message="今天15点从北京到天津，我只能走五百米，别给我安排出租车"), now)
    assert drafts[0].max_walk_m == 500 and Mode.taxi in drafts[0].preferences.excluded_modes


async def test_followup_preferences_replan_and_preserve_trip_limits_without_persisting(
    service, now, monkeypatch
):
    service.settings.amap_api_key = SecretStr("test")
    drafts = []

    async def plan(self, draft):
        drafts.append(draft)
        return self.report

    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    first = await service.chat(
        ChatRequest(message="明天下午3点从北京到天津，预算100元，步行不超过1公里，不坐飞机"), now
    )
    for query in ("不要打车", "预算80元", "可以打车"):
        response = await service.chat(ChatRequest(message=query, session_id=first.session_id), now)
        assert response.status != "memory"
    assert len(drafts) == 4
    assert Mode.taxi in drafts[1].preferences.excluded_modes
    assert Mode.taxi in drafts[2].preferences.excluded_modes
    assert Mode.taxi not in drafts[3].preferences.excluded_modes
    assert all(Mode.flight in d.preferences.excluded_modes and d.max_walk_m == 1000 for d in drafts)
    assert drafts[-1].budget_cents == 8000 and drafts[-1].depart_after.day == 15
    assert not service.repo.preferences().excluded_modes


async def test_multistop_model_limits_and_synonym_exclusions_reach_planner(service, now, monkeypatch):
    service.settings.amap_api_key = SecretStr("test")
    service.settings.llm_api_key = SecretStr("test")
    service.settings.llm_base_url = "https://example.invalid"
    service.settings.llm_model = "test"
    drafts = []

    async def model(*args):
        return ItineraryDraft(
            origin="北京",
            depart_after=now,
            max_walk_m=500,
            max_transfers=1,
            preferences=Preferences(excluded_modes=[Mode.taxi]),
            stops=[
                ItineraryStop(
                    label="演唱会",
                    locations=["天津"],
                    start_at=now.replace(hour=19),
                    requires_start_time=True,
                    duration_min=120,
                ),
                ItineraryStop(locations=["广州"], duration_min=0),
            ],
        )

    async def plan(self, draft):
        drafts.append(draft)
        return self.report

    monkeypatch.setattr(service.llm, "parse_itinerary", model)
    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    await service.chat(
        ChatRequest(message="从北京出发，去天津看演唱会，然后去广州，只能走五百米，别安排出租车，最多换一次"),
        now,
    )
    assert drafts[0].max_walk_m == 500 and drafts[0].max_transfers == 1
    assert Mode.taxi in drafts[0].preferences.excluded_modes


async def test_destination_and_relative_time_corrections_are_not_overwritten_by_history(
    service, now, monkeypatch
):
    service.settings.amap_api_key = SecretStr("test")
    drafts = []

    async def plan(self, draft):
        drafts.append(draft)
        return self.report

    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    first = await service.chat(ChatRequest(message="明天15点从北京到天津，预算100元，不要打车"), now)
    service.settings.llm_api_key = SecretStr("test")
    service.settings.llm_base_url = "https://example.invalid"
    service.settings.llm_model = "test"

    async def model(context, budget):
        previous = context["previous"]
        return ParsedIntent(
            origin="北京",
            destination="保定",
            depart_after=(now.replace(hour=16) + timedelta(days=1)).isoformat(),
            depart_before=(now.replace(hour=16) + timedelta(days=1)).isoformat(),
            arrive_by=previous["arrive_by"],
            budget_cents=10000,
            excluded_modes=["taxi"],
        )

    monkeypatch.setattr(service.llm, "parse", model)
    await service.chat(
        ChatRequest(message="目的地改成保定，出发推迟一小时", session_id=first.session_id), now
    )
    assert drafts[-1].stops[0].locations == ["保定"] and drafts[-1].depart_after.hour == 16
    assert drafts[-1].budget_cents == 10000 and Mode.taxi in drafts[-1].preferences.excluded_modes


def test_multistop_rule_defaults_do_not_erase_richer_model_limits():
    known = rule_draft("从北京出发，去天津看演唱会，然后去广州", Preferences())
    model = known.model_copy(deep=True)
    model.max_walk_m = 500
    assert preserve_known_fields(model, known, None).max_walk_m == 500


def evidence(at, duration=60, cost=500):
    end = at + timedelta(minutes=duration)
    return RouteEvidence(
        origin="A",
        destination="B",
        departure=at,
        arrival=end,
        cost_cents=cost,
        queried_at=at,
        steps=[
            RouteStep(mode=Mode.metro, name="测试线", origin="A", destination="B", departure=at, arrival=end)
        ],
    )


async def test_reverse_departure_is_requeried_and_late_results_excluded(service, now, monkeypatch):
    planner = IntelligentPlanner(service.registry, monotonic() + 10)
    calls = []
    deadline = now.replace(hour=16)

    async def routes(a, b, at, strategy="1"):
        calls.append(at)
        # Later traffic takes longer. Blindly shifting the original timeline would produce a false plan.
        return [evidence(at, 120 if at.hour >= 14 else 60)]

    monkeypatch.setattr(planner, "routes", routes)
    result = await planner.plan(
        ItineraryDraft(
            origin="A",
            depart_after=now,
            depart_before=deadline - timedelta(seconds=1),
            arrive_by=deadline,
            arrival_priority=True,
            stops=[ItineraryStop(locations=["B"], duration_min=0)],
        )
    )
    assert len(calls) >= 2 and any(t.hour == 14 for t in calls)
    assert result.options and all(o.ready <= deadline for o in result.options)
    assert "16:00前到达" in describe_report(result) and result.rejected["超出最终到达期限"]


async def test_reverse_search_refines_first_service_wait_out_of_departure_estimate(service, now, monkeypatch):
    planner = IntelligentPlanner(service.registry, monotonic() + 10)
    start = now.replace(hour=0)
    deadline = now.replace(hour=16)

    async def routes(a, b, at, strategy="1"):
        return [evidence(at, 420 if at.hour < 6 else 60)]

    monkeypatch.setattr(planner, "routes", routes)
    report = await planner.plan(
        ItineraryDraft(
            origin="A",
            depart_after=start,
            depart_before=deadline - timedelta(seconds=1),
            arrive_by=deadline,
            arrival_priority=True,
            stops=[ItineraryStop(locations=["B"], duration_min=0)],
        )
    )
    assert report.options[0].routes[0].departure == now.replace(hour=14, minute=45)
    assert report.options[0].ready == now.replace(hour=15, minute=45)


def test_numeric_hard_limits_do_not_become_budget_or_soft_preferences(now):
    c = parse("明天15点从北京到天津，步行不超过1.5公里，骑行最多2公里，最多1次换乘", Preferences(), now)
    assert c.max_walk_m == 1500 and c.max_bike_m == 2000 and c.max_transfers == 1
    assert c.budget_cents is None
    assert parse("明天15点从北京到天津，少换乘", Preferences(), now).max_transfers is None


@pytest.mark.parametrize("distance,meters", [("五百米", 500), ("一千五百米", 1500), ("一点五公里", 1500)])
def test_chinese_distance_and_transfer_caps_survive_offline_fallback(now, distance, meters):
    c = parse(f"明天15点从北京到天津，只能走{distance}，最多换一次", Preferences(), now)
    assert c.max_walk_m == meters and c.max_transfers == 1 and c.budget_cents is None


async def test_model_failure_does_not_relax_chinese_hard_cap(service, now, monkeypatch):
    service.settings.amap_api_key = SecretStr("test")
    service.settings.llm_api_key = SecretStr("test")
    service.settings.llm_base_url = "https://example.invalid"
    service.settings.llm_model = "test"
    drafts = []

    async def broken(*args):
        raise ValueError("invalid_model_constraints")

    async def plan(self, draft):
        drafts.append(draft)
        return self.report

    monkeypatch.setattr(service.llm, "parse", broken)
    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    await service.chat(ChatRequest(message="明天从北京到天津，下午4点到，只能走五百米，不要打车"), now)
    assert drafts[0].max_walk_m == 500 and Mode.taxi in drafts[0].preferences.excluded_modes
    assert drafts[0].arrive_by.hour == 16 and drafts[0].arrival_priority


def test_multi_stop_limits_survive_model_omission():
    known = rule_draft(
        "2026年9月26日15点从北京出发，19点在天津奥体看演唱会，然后吃海底捞，然后去广州，步行不超过1公里，最多2次换乘",
        Preferences(),
    )
    predicted = known.model_copy(deep=True)
    predicted.max_walk_m, predicted.max_transfers = 5000, None
    merged = preserve_known_fields(predicted, known, None)
    assert merged.max_walk_m == 1000 and merged.max_transfers == 2


@pytest.mark.parametrize("limit", ["walking", "transfers", "cycling"])
async def test_real_candidate_filters_hard_limits(service, now, monkeypatch, limit):
    planner = IntelligentPlanner(service.registry, monotonic() + 10)

    async def routes(a, b, at, strategy="1"):
        route = evidence(at)
        step = route.steps[0].model_copy(
            update={
                "mode": {"walking": Mode.walk, "transfers": Mode.bus, "cycling": Mode.shared_bike}[limit],
                "distance_m": 1500,
            }
        )
        route.steps.insert(0, step)
        return [route]

    monkeypatch.setattr(planner, "routes", routes)
    report = await planner.plan(
        ItineraryDraft(
            origin="A",
            depart_after=now,
            arrive_by=now + timedelta(hours=6),
            max_walk_m=1000,
            max_transfers=0,
            stops=[ItineraryStop(locations=["B"], duration_min=0)],
        )
    )
    assert not report.options
    assert any("限制" in reason or "硬上限" in reason or "骑行接受度" in reason for reason in report.rejected)
