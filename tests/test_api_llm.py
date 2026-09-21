"""API contracts and model-budget boundaries."""

import pytest
from fastapi.testclient import TestClient
from src.api.app import create_app
from src.agent.budget import Budget
from src.errors import TokenLimit


def test_api_session_lifecycle(service):
    with TestClient(create_app(service.settings, service)) as client:
        assert client.get("/health").json()["data_mode"] == "demo"
        sid = client.post("/api/v1/sessions").json()["session_id"]
        result = client.post("/api/v1/chat", json={"session_id": sid, "message": "高铁能带充电宝吗"})
        assert result.status_code == 200
        assert len(client.get(f"/api/v1/sessions/{sid}").json()["messages"]) == 2
        assert client.post("/api/v1/chat", json={"message": ""}).status_code == 422
        assert client.post("/api/v1/chat", json={"message": "hi", "session_id": "missing"}).status_code == 404
        assert client.delete(f"/api/v1/sessions/{sid}").status_code == 200
        assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
        assert client.put("/api/v1/preferences", json={"cycling_acceptance": 2}).status_code == 200
        assert client.get("/api/v1/preferences").json()["cycling_acceptance"] == 2
        assert client.delete("/api/v1/preferences").status_code == 200


def test_api_optional_auth(service):
    from pydantic import SecretStr

    settings = service.settings.model_copy(update={"api_access_token": SecretStr("test-only")})
    with TestClient(create_app(settings, service)) as client:
        assert client.get("/api/v1/preferences").status_code == 401
        assert (
            client.get("/api/v1/preferences", headers={"Authorization": "Bearer test-only"}).status_code
            == 200
        )


def test_token_reservation_happens_before_call():
    budget = Budget(token_limit=300)
    reservation = budget.reserve("abc", 100)
    budget.reconcile(reservation, 10)
    with pytest.raises(TokenLimit):
        budget.reserve("second", 100)
    assert budget.used <= 300 and budget.actual == 10


async def test_invalid_llm_output_retries_then_uses_rules(service, now, monkeypatch):
    from pydantic import SecretStr

    service.settings.llm_base_url = "https://invalid.test/v1"
    service.settings.llm_model = "test"
    service.settings.llm_api_key = SecretStr("test")
    count = [0]

    async def invalid(*args, **kwargs):
        count[0] += 1
        return {"choices": [{"message": {"content": "not JSON"}}]}

    monkeypatch.setattr(service.llm, "completion", invalid)
    result = await service.chat(
        __import__("src.domain", fromlist=["ChatRequest"]).ChatRequest(
            message="明天晚上从北京海淀到天津滨海新区，预算50元，不要打车"
        ),
        now,
    )
    assert result.plans and result.constraints.budget_cents == 5000
    assert count[0] >= 2 and "ValueError" in result.metadata["errors"]


async def test_llm_transport_usage_cache_and_no_call_over_budget(service, monkeypatch):
    import httpx

    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 12}}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    service.settings.llm_base_url = "https://llm.test/v1"
    budget = Budget()
    await service.llm.completion({"messages": []}, budget, max_tokens=20)
    assert budget.actual == 12 and len(attempts) == 1
    await service.llm.completion({"messages": []}, budget, max_tokens=20)
    assert len(attempts) == 1
    with pytest.raises(TokenLimit):
        await service.llm.completion({"messages": [{"content": "new"}]}, Budget(token_limit=64))
    assert len(attempts) == 1
