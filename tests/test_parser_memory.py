"""Explicit constraints and temporary/long-term preference semantics."""

import pytest
from src.agent.parser import parse
from src.domain import Mode, Preferences
from src.errors import NeedsClarification
from src.memory import merge_preferences, preference_patch, should_remember


def test_natural_constraints(now):
    text = "明天晚上从北京海淀到天津滨海新区，预算100以内，能骑共享单车，不要打车"
    p = merge_preferences(Preferences(), preference_patch(text, Preferences()))
    c = parse(text, p, now)
    assert c.depart_after.day == 15 and c.depart_after.hour == 18
    assert c.depart_before.hour == 23 and c.depart_before.minute == 59
    assert c.budget_cents == 10000 and c.excluded_modes == [Mode.taxi] and c.cycling_acceptance == 2


@pytest.mark.parametrize(
    "query",
    [
        "从北京到天津",
        "明天25:00从北京到天津",
        "明天晚上从北京到天津，预算一百元",
        "明天23:00从北京到天津，最晚22:00到",
        "明天22:00从北京到天津，必须23点到",
    ],
)
def test_ambiguous_hard_constraints_require_clarification(now, query):
    with pytest.raises(NeedsClarification):
        parse(query, Preferences(), now)


def test_preferences_can_be_revoked_and_temporary():
    p = Preferences(excluded_modes=[Mode.taxi])
    assert preference_patch("这次可以打车", p)["excluded_modes"] == []
    assert not should_remember("这次不要打车")
    assert should_remember("以后不要打车，请记住")


def test_overnight_arrival_and_zero_budget(now):
    c = parse("明天23:00从北京到天津，最晚次日01:00到，预算0元", Preferences(), now)
    assert c.arrive_by.day == 16 and c.budget_cents == 0


def test_session_followup_keeps_route(now):
    c = parse("明天晚上从北京海淀到天津滨海新区，预算100元", Preferences(), now)
    changed = parse("预算50元", Preferences(), now, c)
    assert (
        changed.origin == c.origin and changed.depart_after == c.depart_after and changed.budget_cents == 5000
    )
