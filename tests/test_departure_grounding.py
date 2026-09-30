"""Regression: user departure evidence wins even if both model passes get it wrong."""

import json
from datetime import timedelta

import pytest
from pydantic import SecretStr

from src.agent.departure_grounding import departure_fact, grounded_patch
from src.agent.intelligent import IntelligentPlanner, PlanningReport
from src.domain import ChatRequest, ChatResponse, ItineraryDraft, ItineraryStop


QUERY = "我要在今晚10:30从鸟巢回北京印刷学院，帮我规划路线"


@pytest.mark.parametrize(
    "query", [QUERY, "今晚十点半从鸟巢回北京印刷学院", "今晚22：30出发", "今晚出发时间是22:30"]
)
def test_departure_literal_variants(query, now):
    fact = departure_fact(query, [], now)
    # The final variant leaves the period before the named-time phrase, but has
    # an unambiguous 24-hour clock.
    assert fact and fact.at == now.replace(hour=22, minute=30)


def test_short_correction_inherits_evening_from_user(now):
    fact = departure_fact("10:30出发", [QUERY], now)
    assert fact.at.hour == 22 and fact.period_source == QUERY
    assert departure_fact("10:30出发", [], now) is None


def test_tomorrow_is_not_applied_twice_when_inheriting(now):
    reference = (now + timedelta(days=1)).replace(hour=22, minute=30)
    fact = departure_fact("10:30出发", ["明晚10:30从鸟巢回学校"], now, reference)
    assert fact.at == reference


@pytest.mark.parametrize(
    "query", ["周五22:30出发", "10月1日22:30出发", "22:00到22:30出发", "22:00-22:30出发"]
)
def test_unresolved_dates_and_time_ranges_stay_with_semantic_parser(query, now):
    assert departure_fact(query, [], now) is None


def test_explicit_calendar_is_not_replaced_with_today(now):
    fact = departure_fact("2026年10月1日22:30从北京回天津", [], now)
    assert fact.at == now.replace(year=2026, month=10, day=1, hour=22, minute=30)


def test_changed_departure_removes_deadline_wrongly_copied_from_old_departure(now):
    deadline = now.replace(hour=22, minute=30)
    base = ItineraryDraft(depart_after=now, arrive_by=deadline, arrival_priority=True)
    fact = departure_fact("改成23点出发", [QUERY], now)
    patch = grounded_patch(base, {}, fact, [QUERY, "改成23点出发"], now)
    assert patch["arrive_by"] is None and patch["arrival_priority"] is False
    assert patch["depart_after"] == now.replace(hour=23, minute=0).isoformat()


@pytest.mark.parametrize(
    "query", ["不是22:30出发，是23点到", "22:30前出发", "最晚22:30出发", "大约22:30出发", "明天22:30到学校"]
)
def test_non_exact_or_arrival_not_overridden(query, now):
    assert departure_fact(query, [], now) is None


def test_real_arrival_deadline_is_not_erased(now):
    at = now.replace(hour=22, minute=30)
    fact = departure_fact("22:30出发", [], now)
    base = ItineraryDraft(depart_after=now, arrive_by=at, arrival_priority=True)
    patch = grounded_patch(base, {}, fact, ["今天22:30到学校", "22:30出发"], now)
    assert "arrive_by" not in patch  # Keep the real conflict for validation, not silently clear it.
    assert patch["arrival_priority"] is False


@pytest.mark.parametrize("corrupt_history", [False, True])
async def test_two_turn_departure_even_when_models_repeat_arrival_error(
    service, now, monkeypatch, corrupt_history
):
    service.settings.conversation_agent = True
    service.settings.llm_base_url = "https://llm.test/v1"
    service.settings.llm_model = "test"
    service.settings.llm_api_key = SecretStr("test")
    service.settings.amap_api_key = SecretStr("test")
    sid = service.repo.new_session()
    departure = now.replace(hour=22, minute=30)
    planner_inputs = []

    async def completion(body, *args, **kwargs):
        # BOTH initial extraction and semantic audit deliberately propose the wrong role.
        patch = {"depart_after": None, "arrive_by": departure.isoformat(), "arrival_priority": True}
        if body["messages"][0]["content"].startswith("Audit"):
            value = {"patch": patch}
        else:
            value = {
                "action": "plan",
                "mode": "update",
                "patch": {
                    **patch,
                    "origin": "鸟巢",
                    "stops": [{"locations": ["北京印刷学院"], "duration_min": 0}],
                },
            }
        return {"choices": [{"message": {"content": json.dumps(value)}}]}

    async def plan(self, draft):
        planner_inputs.append(draft)
        self.report = PlanningReport(
            departure_at=draft.depart_after,
            arrival_priority=draft.arrival_priority,
            arrive_by=draft.arrive_by,
        )
        return self.report

    monkeypatch.setattr(service.llm, "completion", completion)
    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    if corrupt_history:
        # Exactly the state persisted by the faulty production reply: wrong morning
        # departure AND an invented evening arrival deadline.
        broken = ItineraryDraft(
            origin="鸟巢",
            depart_after=now.replace(hour=10, minute=30),
            arrive_by=departure,
            arrival_priority=True,
            stops=[ItineraryStop(locations=["北京印刷学院"], duration_min=0)],
        )
        service.repo.save_turn(
            QUERY,
            ChatResponse(
                session_id=sid,
                status="degraded",
                answer="22:30前到达",
                metadata={"itinerary_draft": broken.model_dump(mode="json")},
            ),
        )
    else:
        await service.chat(ChatRequest(session_id=sid, message=QUERY), now)
    result = await service.chat(ChatRequest(session_id=sid, message="10:30出发"), now + timedelta(minutes=1))
    assert planner_inputs
    for draft in planner_inputs:
        assert draft.depart_after == departure and draft.depart_before == departure
        assert draft.arrive_by is None and not draft.arrival_priority
    assert "前到达" not in result.answer
    assert result.metadata["conversation"]["time_grounding"]["period_source"] == QUERY


async def test_exact_departure_blocks_reverse_search_even_with_stale_flag(service, now, monkeypatch):
    draft = ItineraryDraft(
        origin="起点",
        depart_after=now,
        depart_before=now,
        arrive_by=now + timedelta(hours=3),
        arrival_priority=True,
        stops=[ItineraryStop(locations=["终点"], duration_min=0)],
    )
    planner = IntelligentPlanner(service.registry, 9999999999)
    queried = []

    async def forward(origin, destination, at, strategy="1"):
        queried.append(at)
        return []

    async def reverse(*args):
        pytest.fail("Exact departure must never use reverse departure search")

    monkeypatch.setattr(planner, "routes", forward)
    monkeypatch.setattr(planner, "arrival_routes", reverse)
    report = await planner.plan(draft)
    assert queried and all(at == now for at in queried)
    assert report.arrival_priority is False
