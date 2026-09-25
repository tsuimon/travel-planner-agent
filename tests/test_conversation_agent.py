"""Model-driven actions are tested independently of any keyword parser or live API."""

import json
from datetime import timedelta

import pytest
from pydantic import SecretStr, ValidationError

from src.agent.conversation import active_draft
from src.agent.conversation_actions import ACTION, patch_draft
from src.agent.intelligent import IntelligentPlanner, PlanningReport
from src.agent.route_probe import probe_route
from src.domain import ChatRequest, ChatResponse, ItineraryDraft, ItineraryStop, Preferences, Mode
from src.errors import TokenLimit


@pytest.fixture
def model_service(service):
    service.settings.conversation_agent = True
    service.settings.llm_base_url = "https://llm.test/v1"
    service.settings.llm_model = "test"
    service.settings.llm_api_key = SecretStr("test")
    service.settings.amap_api_key = SecretStr("test")
    return service


def seed(service, now):
    sid = service.repo.new_session()
    draft = ItineraryDraft(
        origin="鸟巢",
        depart_after=now.replace(hour=22, minute=40),
        stops=[ItineraryStop(locations=["北京印刷学院"], duration_min=0)],
        budget_cents=10000,
        preferences=Preferences(excluded_modes=[Mode.taxi]),
    )
    service.repo.save_turn(
        "今天22:40从鸟巢去北京印刷学院，不要打车，预算100",
        ChatResponse(
            session_id=sid,
            status="degraded",
            answer="后续换乘赶不上",
            metadata={"itinerary_draft": draft.model_dump(mode="json")},
        ),
    )
    return sid, draft


def decisions(service, monkeypatch, actions):
    calls = []

    async def completion(body, *args, **kwargs):
        if body["messages"][0]["content"].startswith("Audit"):
            return {"choices": [{"message": {"content": '{"patch":{}}'}}]}
        calls.append(json.loads(body["messages"][-1]["content"]))
        value = actions[len(calls) - 1]
        return {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}

    monkeypatch.setattr(service.llm, "completion", completion)
    return calls


async def test_followup_queries_without_mutating_trip(model_service, now, monkeypatch):
    service = model_service
    sid, draft = seed(service, now)
    # A failed reply must not erase the older trip.
    service.repo.save_turn("再试一下", ChatResponse(session_id=sid, status="degraded", answer="暂不可用"))
    actions = [
        {
            "action": "probe",
            "origin": "鸟巢",
            "destination": "北京印刷学院",
            "at": draft.depart_after.isoformat(),
            "line": "10号线",
        },
        {"action": "reply", "message": "保证23:55出发一定赶上"},
    ]
    calls = decisions(service, monkeypatch, actions)

    async def fake_probe(*args):
        return dict(
            origin="鸟巢",
            destination="北京印刷学院",
            queried_departure=draft.depart_after.isoformat(),
            source="https://example.test/evidence",
            checked_routes=[],
            rejected=["outside_operating_hours"],
            windows=[
                dict(
                    line="10号线外环",
                    boarding_station="北土城",
                    station_last="0001",
                    line_terminal_last="2300",
                    estimated_boarding=None,
                )
            ],
            limitations="全程尚未核验",
        )

    monkeypatch.setattr("src.agent.conversation.probe_route", fake_probe)
    result = await service.chat(
        ChatRequest(session_id=sid, message="10号线最后一班几点，提前到什么时候能赶上"), now
    )
    assert result.metadata["conversation"]["actions"] == ["probe", "reply"]
    assert result.metadata["itinerary_draft"] == draft.model_dump(mode="json")
    assert "北土城" in result.answer and "00:01" in result.answer
    assert "23:55" not in result.answer and "保证" not in result.answer
    assert calls[0]["trip"]["origin"] == "鸟巢"
    assert calls[1]["observation"]["checked_routes"] == []


@pytest.mark.parametrize("mode,expected", [("preview", 22), ("update", 21)])
async def test_hypothetical_vs_edit_preserves_constraints(model_service, now, monkeypatch, mode, expected):
    sid, draft = seed(model_service, now)
    time = now.replace(hour=21).isoformat()
    decisions(model_service, monkeypatch, [{"action": "plan", "mode": mode, "patch": {"depart_after": time}}])
    used = []

    async def plan(self, itinerary):
        used.append(itinerary)
        self.report = PlanningReport(questions=["测试没有交通数据"])
        return self.report

    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    result = await model_service.chat(ChatRequest(session_id=sid, message="如果改21点呢"), now)
    saved = ItineraryDraft.model_validate(result.metadata["itinerary_draft"])
    assert saved.depart_after.hour == expected
    assert used[0].depart_after.hour == 21
    assert used[0].budget_cents == 10000 and used[0].preferences.excluded_modes == [Mode.taxi]
    assert saved.stops == draft.stops


async def test_arrival_deadline_is_not_departure(model_service, now, monkeypatch):
    decisions(
        model_service,
        monkeypatch,
        [
            {
                "action": "plan",
                "mode": "new",
                "patch": {
                    "origin": "鸟巢",
                    "stops": [{"locations": ["北京印刷学院"], "duration_min": 0}],
                    "arrive_by": now.replace(hour=16).isoformat(),
                    "arrival_priority": True,
                },
            }
        ],
    )
    drafts = []

    async def plan(self, itinerary):
        drafts.append(itinerary)
        self.report = PlanningReport()
        return self.report

    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    await model_service.chat(ChatRequest(message="下午4点到学校"), now)
    assert drafts[0].arrive_by.hour == 16 and drafts[0].depart_after == now
    assert drafts[0].arrival_priority


async def test_missing_context_asks_targeted_question(model_service, now, monkeypatch):
    decisions(
        model_service,
        monkeypatch,
        [{"action": "reply", "message": "哪个城市、哪个站和方向？", "clarification": True}],
    )
    result = await model_service.chat(ChatRequest(message="10号线末班几点"), now)
    assert result.status == "clarification" and result.metadata["conversation"]["actions"] == ["reply"]


async def test_invalid_action_repaired_without_executing_arbitrary_tool(model_service, now, monkeypatch):
    calls = decisions(
        model_service,
        monkeypatch,
        [
            {"action": "lookup", "tool": "shell", "arguments": {}},
            {"action": "reply", "message": "请说明你要查询的地点。"},
        ],
    )
    result = await model_service.chat(ChatRequest(message="hello"), now)
    assert len(calls) == 2 and "invalid_conversation_action" in result.metadata["errors"]
    assert result.metadata["conversation"]["actions"] == ["reply"]


async def test_model_budget_failure_preserves_trip_without_rule_misrouting(model_service, now, monkeypatch):
    sid, draft = seed(model_service, now)

    async def limited(*args, **kwargs):
        raise TokenLimit()

    monkeypatch.setattr(model_service.llm, "completion", limited)
    result = await model_service.chat(ChatRequest(session_id=sid, message="提前一点能赶上么"), now)
    assert result.status == "degraded" and "行程修改" not in result.answer
    assert result.metadata["itinerary_draft"] == draft.model_dump(mode="json")


def test_patch_preserves_nested_preferences_and_validates_hard_constraints():
    draft = ItineraryDraft(preferences=Preferences(excluded_modes=[Mode.taxi]), max_walk_m=500)
    result = patch_draft(draft, {"preferences": {"budget_preference": "economy"}})
    assert result.preferences.excluded_modes == [Mode.taxi] and result.max_walk_m == 500
    with pytest.raises(ValidationError):
        patch_draft(draft, {"max_walk_m": -1})
    with pytest.raises(ValidationError):
        ACTION.validate_python({"action": "lookup", "tool": "preferences", "arguments": {}})


async def test_probe_retains_prefix_but_does_not_claim_full_connection(now):
    def line(name, start, end):
        return {
            "name": name,
            "type": "地铁",
            "departure_stop": {"name": name + "上车站"},
            "arrival_stop": {"name": name + "下车站"},
            "station_start_time": start,
            "station_end_time": end,
            "cost": {"duration": "600"},
        }

    body = {
        "status": "1",
        "route": {
            "transits": [
                {
                    "segments": [
                        {"bus": {"buslines": [line("10号线", "0500", "0001")]}},
                        {"bus": {"buslines": [line("4号线", "0500", "2200")]}},
                    ]
                }
            ]
        },
    }

    class Planner:
        report = PlanningReport()

        async def resolve(self, name):
            return {"location": "116,39", "citycode": "010"}

        async def call(self, *args):
            return body

    result = await probe_route(Planner(), "起点", "终点", now.replace(hour=22, minute=40), "10号线")
    assert result["checked_routes"] == []
    assert result["windows"][0]["estimated_boarding"].endswith("22:45:00+08:00")
    assert result["windows"][0]["last_boarding_at"].startswith((now + timedelta(days=1)).strftime("%Y-%m-%d"))
    assert "outside_operating_hours" in result["rejected"]


def test_empty_new_trip_does_not_resurrect_old_context(now):
    old = ItineraryDraft(origin="旧起点").model_dump(mode="json")
    history = [
        {"payload": {"metadata": {"itinerary_draft": old}}},
        {"payload": {"metadata": {"itinerary_draft": {}}}},
    ]
    assert active_draft(history, Preferences()).origin is None


def test_event_duration_is_computed_from_explicit_start_end(now):
    stop = ItineraryStop(start_at=now.replace(hour=19, minute=30), end_at=now.replace(hour=22))
    assert stop.duration_min == 150
    with pytest.raises(ValidationError):
        ItineraryStop(start_at=stop.start_at, end_at=stop.end_at, duration_min=180)


async def test_audit_corrects_invented_departure_before_planning(model_service, now, monkeypatch):
    start = now.replace(hour=15)
    calls = []

    async def completion(body, *args, **kwargs):
        audit = body["messages"][0]["content"].startswith("Audit")
        calls.append(audit)
        value = (
            {"patch": {"depart_after": start.isoformat()}}
            if audit
            else {
                "action": "plan",
                "mode": "new",
                "patch": {
                    "origin": "北京",
                    "depart_after": start.replace(minute=30).isoformat(),
                    "stops": [{"locations": ["天津"], "duration_min": 0}],
                },
            }
        )
        return {"choices": [{"message": {"content": json.dumps(value)}}]}

    async def plan(self, draft):
        assert draft.depart_after == start
        self.report = PlanningReport()
        return self.report

    monkeypatch.setattr(model_service.llm, "completion", completion)
    monkeypatch.setattr(IntelligentPlanner, "plan", plan)
    result = await model_service.chat(ChatRequest(message="今天15点从北京出发去天津"), now)
    assert calls == [False, True]
    assert result.metadata["itinerary_draft"]["depart_after"] == start.isoformat()


async def test_incomplete_model_draft_without_map_never_enters_solver(model_service, now, monkeypatch):
    model_service.settings.amap_api_key = SecretStr("")
    decisions(model_service, monkeypatch, [{"action": "plan", "mode": "new", "patch": {"origin": "北京"}}])
    result = await model_service.chat(ChatRequest(message="我从北京出发"), now)
    assert result.status == "clarification"
    assert result.metadata["itinerary_draft"]["origin"] == "北京"
