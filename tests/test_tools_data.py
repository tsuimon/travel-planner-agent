"""Cache isolation, persistence and retries under real async deadlines."""

import asyncio
from time import monotonic
from src.data.cache import TTLCache
from src.data.repository import Repository
from src.domain import Preferences
from src.tools.base import BaseTool
from src.tools.providers import WebArgs


def test_ttl_and_isolation():
    clock = [0]
    cache = TTLCache(2, lambda: clock[0])
    cache.set("a", {"x": []}, 2)
    cache.get("a")["x"].append(1)
    assert cache.get("a") == {"x": []}
    clock[0] = 2
    assert cache.get("a") is None


def test_preferences_survive_reopen(tmp_path):
    url = f"sqlite:///{tmp_path}/db.sqlite"
    one = Repository(url)
    one.save_preferences(Preferences(cycling_acceptance=2))
    one.engine.dispose()
    two = Repository(url)
    assert two.preferences().cycling_acceptance == 2
    two.engine.dispose()


async def test_tool_retries_and_cached_fallback(service):
    class Flaky(BaseTool):
        name, args_model = "flaky", WebArgs
        count = 0
        fail = False

        async def execute(self, args):
            self.count += 1
            if self.fail or self.count < 3:
                raise RuntimeError("fail")
            return {"result": args.query}

    tool = Flaky(service.settings, TTLCache(), service.repo)
    result = await tool.call({"query": "q"}, monotonic() + 2)
    assert result.success and result.attempts == 3
    tool.fail = True
    cached = await tool.call({"query": "q"}, monotonic() + 2, refresh=True)
    assert cached.success and cached.cached and cached.error


async def test_tool_timeout_and_invalid_args(service):
    class Slow(BaseTool):
        name, args_model = "slow", WebArgs

        async def execute(self, args):
            await asyncio.sleep(2)
            return {}

    tool = Slow(service.settings.model_copy(update={"tool_timeout": 0.01}), TTLCache())
    started = monotonic()
    result = await tool.call({"query": "q"}, started + 1)
    assert not result.success and monotonic() - started < 0.5
    assert not (await tool.call({}, monotonic() + 1)).success


def test_registry_exports_real_schemas(service):
    schemas = service.registry.schemas()
    assert len(schemas) == 10
    assert all(s["function"]["parameters"]["type"] == "object" for s in schemas)


async def test_live_never_falls_back_to_demo(service, now):
    tool = service.registry.tools["transit_query"]
    tool.settings = tool.settings.model_copy(update={"data_mode": "live", "provider_url": ""})
    result = await tool.call(
        {"origin": "a", "destination": "b", "depart_after": now, "arrive_by": now}, monotonic() + 2
    )
    assert not result.success and result.data == {}


async def test_expired_cache_not_used_after_retry(service):
    clock = [0.0]

    class Expires(BaseTool):
        name, args_model, ttl = "expires", WebArgs, 1
        fail = False

        async def execute(self, args):
            if self.fail:
                clock[0] = 2
                raise RuntimeError("failed")
            return {"ok": True}

    tool = Expires(service.settings, TTLCache(clock=lambda: clock[0]))
    assert (await tool.call({"query": "a"}, monotonic() + 1)).success
    tool.fail = True
    result = await tool.call({"query": "a"}, monotonic() + 1, refresh=True)
    assert not result.success and not result.cached


async def test_gateway_http_schema_validation_and_retry(service, now, monkeypatch):
    import httpx
    from src.search.sample import sample_network

    tool = service.registry.tools["transit_query"]
    tool.settings = tool.settings.model_copy(
        update={"data_mode": "live", "provider_url": "https://provider.test"}
    )
    valid = sample_network(now).model_dump(mode="json")
    for edge in valid["edges"]:
        edge["demo"] = False
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, json={"invalid": True} if len(attempts) == 1 else valid)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    result = await tool.call(
        {"origin": "a", "destination": "b", "depart_after": now, "arrive_by": now}, monotonic() + 2
    )
    assert result.success and result.attempts == 2
    assert str(attempts[-1].url) == "https://provider.test/transit_query"
    assert all(not edge["demo"] for edge in result.data["edges"])
