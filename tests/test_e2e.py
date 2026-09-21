"""Eight required end-to-end scenarios, all reproducible without credentials."""

import asyncio
from time import monotonic
from src.domain import ChatRequest, Mode, Preferences


async def test_e2e_001_night(service, night, now):
    r = await service.chat(ChatRequest(message="夜间规划", constraints=night), now)
    assert r.status == "ok" and len(r.plans) >= 2
    assert any(Mode.metro in [x.mode for x in p.legs] for p in r.plans)
    assert any([x.mode for x in p.legs] == [Mode.taxi] for p in r.plans)


async def test_e2e_002_budget(service, now):
    r = await service.chat(ChatRequest(message="明天晚上从北京海淀到天津滨海新区，预算50元，不要打车"), now)
    assert r.plans and all(p.total_cost_cents <= 5000 for p in r.plans)
    assert all(x.mode != Mode.taxi for p in r.plans for x in p.legs)


async def test_e2e_003_multiple_transfers(service, now):
    r = await service.chat(ChatRequest(message="明天上午8:00从甲县到乙县，预算100元"), now)
    assert r.plans and r.plans[0].transfers == 2
    assert [x.mode for x in r.plans[0].legs] == [Mode.county_bus, Mode.high_speed_rail, Mode.county_bus]


async def test_e2e_004_policy(service, now):
    r = await service.chat(ChatRequest(message="高铁能带充电宝吗"), now)
    assert r.status == "policy" and "100" in r.answer and "12306.cn" in r.answer
    assert r.sources and r.sources[0]["reviewed"]
    other = await service.chat(ChatRequest(message="飞机能带充电宝吗"), now)
    assert not other.sources and "尚未收录" in other.answer


async def test_e2e_005_memory(service, now):
    first = await service.chat(ChatRequest(message="记住，我不想打车"), now)
    assert first.status == "memory"
    second = await service.chat(ChatRequest(message="明天晚上从北京海淀到天津滨海新区"), now)
    assert second.plans and Mode.taxi in second.constraints.excluded_modes
    await service.chat(ChatRequest(message="这次可以打车"), now)
    assert Mode.taxi in service.repo.preferences().excluded_modes


async def test_e2e_006_failure(service, now, monkeypatch):
    async def fail(args):
        raise RuntimeError("simulated_provider_failure")

    monkeypatch.setattr(service.registry.tools["transit_query"], "execute", fail)
    r = await service.chat(ChatRequest(message="明天晚上从北京海淀到天津滨海新区"), now)
    assert r.status == "degraded" and not r.plans
    assert "transit_query_unavailable" in r.metadata["errors"]


async def test_e2e_007_breakers(service, now, monkeypatch):
    r = await service.chat(ChatRequest(message="明天晚上从北京海淀到天津滨海新区，预算0元"), now)
    assert r.metadata["iterations"] == 6 and not r.plans and "max_iterations" in r.metadata["errors"]

    async def slow(args):
        await asyncio.sleep(2)
        return {}

    service.settings.planning_timeout = 0.03
    monkeypatch.setattr(service.registry.tools["geocode"], "execute", slow)
    started = monotonic()
    r = await service.chat(ChatRequest(message="明天晚上从未知地点到另一个地点"), now)
    assert monotonic() - started < 0.5 and "planning_timeout" in r.metadata["errors"]


async def test_e2e_008_risk(service, night, now):
    c = night.model_copy(
        update={
            "excluded_modes": [Mode.taxi, Mode.walk],
            "depart_after": night.depart_after.replace(hour=23, minute=0),
            "depart_before": night.depart_before.replace(hour=23, minute=0),
        }
    )
    r = await service.chat(ChatRequest(message="风险规划", constraints=c), now)
    assert r.plans
    labels = " ".join(r.plans[0].risks)
    assert all(s in labels for s in ("夜间覆盖不确定", "骑行可能受影响", "仅供参考", "末班"))


async def test_sessions_cannot_leak_route(service, now):
    await service.chat(ChatRequest(message="明天晚上从北京海淀到天津滨海新区"), now)
    r = await service.chat(ChatRequest(message="预算50元"), now)
    assert r.status == "clarification" and not r.plans


async def test_policy_staleness_and_preference_reset(service, now):
    from datetime import date

    answer, sources = service.knowledge.answer("高铁能带充电宝吗", date(2028, 1, 1))
    assert sources[0]["stale"] and "未复核" in answer
    service.repo.save_preferences(Preferences())
    assert service.repo.preferences().excluded_modes == []
