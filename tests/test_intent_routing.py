"""Regression cases for journey requests swallowed by preference extraction."""

import pytest
from pydantic import SecretStr

from src.agent.parser import parse
from src.domain import ChatRequest, Preferences
from src.errors import NeedsClarification
from src.memory import is_preference_only, should_remember


SCREENSHOT_QUERY = (
    "我现在在北京大兴清源路，26号晚上要去天津奥体看演唱会，然后吃海底捞，然后去广州，给我省钱且合理的方案"
)


@pytest.mark.parametrize(
    "text",
    [
        SCREENSHOT_QUERY,
        "我在北京海淀，明天晚上要去天津滨海新区，给我省钱方案",
        "从北京海淀到天津滨海新区，然后去广州，尽量便宜",
        "给我规划一条省钱路线",
        "明天23:00从北京到天津，少换乘",
    ],
)
def test_trip_is_not_preference_only(text):
    assert not is_preference_only(text)
    assert not should_remember(text)


@pytest.mark.parametrize("text", ["记住，我不想打车", "以后少换乘", "我想省钱", "能骑共享单车"])
def test_explicit_preference_still_works(text):
    assert is_preference_only(text)
    assert should_remember(text)


async def test_screenshot_request_clarifies_without_saving_preferences(service, now):
    before = service.repo.preferences()
    result = await service.chat(ChatRequest(message=SCREENSHOT_QUERY), now)
    assert result.status == "clarification" and result.plans == []
    assert "已保存偏好" not in result.answer
    assert all(word in result.answer for word in ("省钱", "年月日", "散场", "用餐", "过夜", "演示数据"))
    assert [s["locations"] for s in result.metadata["itinerary_draft"]["stops"]] == [
        ["天津奥体"],
        ["海底捞"],
        ["广州"],
    ]
    assert service.repo.preferences() == before
    assert service.repo.history(result.session_id)[0]["content"] == SCREENSHOT_QUERY


async def test_no_from_syntax_plans_simple_trip(service, now):
    result = await service.chat(
        ChatRequest(message="我在北京海淀，明天晚上要去天津滨海新区，预算100元，给我省钱方案"), now
    )
    assert result.plans and result.constraints.budget_preference == "economy"
    assert service.repo.preferences().budget_preference == "balanced"


async def test_llm_cannot_collapse_activity_trip_to_single_od(service, now, monkeypatch):
    service.settings.llm_base_url = "https://test.invalid/v1"
    service.settings.llm_model = "test"
    service.settings.llm_api_key = SecretStr("test")

    async def must_not_call(*args, **kwargs):
        pytest.fail("Multi-stop request must not enter the single-OD LLM parser")

    monkeypatch.setattr(service.llm, "parse", must_not_call)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("offline_test")

    monkeypatch.setattr(service.llm, "parse_itinerary", unavailable)
    result = await service.chat(ChatRequest(message=SCREENSHOT_QUERY), now)
    assert result.status == "clarification"


@pytest.mark.parametrize("text", ["26号晚上从北京海淀到天津滨海新区，省钱", "9月26日晚上从北京到天津"])
def test_partial_calendar_date_is_not_silently_today(now, text):
    with pytest.raises(NeedsClarification, match="年月日"):
        parse(text, Preferences(), now)


async def test_complex_request_followup_does_not_reuse_old_trip(service, now):
    first = await service.chat(ChatRequest(message="明天晚上从北京海淀到天津滨海新区"), now)
    await service.chat(ChatRequest(message=SCREENSHOT_QUERY, session_id=first.session_id), now)
    followup = await service.chat(ChatRequest(message="预算500元", session_id=first.session_id), now)
    assert followup.status == "clarification" and not followup.plans
