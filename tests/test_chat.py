import pytest


@pytest.mark.asyncio
async def test_chat_requires_auth(app_client):
    r = await app_client.post("/api/chat", json={"message": "hello"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_chat_flow_persistence(authed_client, monkeypatch):
    # Mock router to avoid real Ollama — use lifespan-scoped router via DI
    from app.ai.ollama import OllamaError

    async def fake_chat(messages, requested_node=None, stream=False, **opts):
        from app.ai.router import RouteResult

        return RouteResult(content="mock reply", actual_node="node1", actual_model="qwen3:1.7b", latency_ms=42)

    router = authed_client.app.state.router  # type: ignore[attr-defined]
    monkeypatch.setattr(router, "chat", fake_chat)

    # Send first message without conversation_id -> creates conversation
    r = await authed_client.post("/api/chat", json={"message": "hello"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["reply"] == "mock reply"
    assert data["actual_node"] == "node1"
    conv_id = data["conversation_id"]

    # Verify conversation persisted
    r2 = await authed_client.get(f"/api/conversations/{conv_id}")
    assert r2.status_code == 200
    msgs = r2.json()["messages"]
    assert len(msgs) == 2  # user + assistant
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["content"] == "mock reply"
    assert msgs[1]["model"] == "qwen3:1.7b"
    assert msgs[1]["node_id"] == "node1"

    # Second message in same conversation
    r3 = await authed_client.post("/api/chat", json={"conversation_id": conv_id, "message": "follow up"})
    assert r3.status_code == 200
    assert r3.json()["conversation_id"] == conv_id

    r4 = await authed_client.get(f"/api/conversations/{conv_id}")
    assert len(r4.json()["messages"]) == 4


@pytest.mark.asyncio
async def test_chat_unknown_node_400(authed_client):
    r = await authed_client.post("/api/chat", json={"message": "hi", "node_id": "nodeX"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_chat_node_failure_returns_502(authed_client, monkeypatch):
    from app.ai.ollama import OllamaError

    async def failing_chat(*a, **kw):
        raise OllamaError("all nodes down")

    router = authed_client.app.state.router  # type: ignore[attr-defined]
    monkeypatch.setattr(router, "chat", failing_chat)
    r = await authed_client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_conversation_isolation(authed_client, monkeypatch):
    async def fake_chat(messages, requested_node=None, stream=False, **opts):
        from app.ai.router import RouteResult

        return RouteResult(content="hi", actual_node="node1", actual_model="qwen3:1.7b", latency_ms=10)

    router = authed_client.app.state.router  # type: ignore[attr-defined]
    monkeypatch.setattr(router, "chat", fake_chat)

    # User A creates conv
    r = await authed_client.post("/api/chat", json={"message": "my private"})
    conv_id = r.json()["conversation_id"]

    # New user B should not see it
    import uuid

    app_client = authed_client  # reuse transport but create new user
    # Create second user via same client but new token? Create fresh client
    from httpx import ASGITransport, AsyncClient

    transport = authed_client._transport if hasattr(authed_client, "_transport") else None
    # Simpler: register a second user via same client with overridden token
    # Use the underlying app
    from app.main import create_app

    # Create a second authed client via fixture logic — manually
    base_url = "http://test"
    # Re-use same app_client's app via closure? Use authed_client's underlying app.
    # Instead just try to fetch with no auth after clearing token then register new
    # We'll directly call register with new user, get new token, and try to fetch conv
    r2 = await authed_client.post("/api/auth/register", json={"username": f"other_{uuid.uuid4().hex[:4]}", "password": "pass123456789", "display_name": "Other"})
    token2 = r2.json()["access_token"]
    # Try to access first user's conversation with token2
    authed_client.headers["Authorization"] = f"Bearer {token2}"
    r3 = await authed_client.get(f"/api/conversations/{conv_id}")
    assert r3.status_code == 404  # not found for other user
