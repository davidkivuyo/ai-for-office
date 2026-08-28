import pytest
from app.ai.router import AIRouter
from app.ai.ollama import OllamaError


@pytest.mark.asyncio
async def test_router_explicit_selection(monkeypatch):
    router = AIRouter()
    # Explicit node exists
    node = router.resolve_node("node1")
    assert node is not None
    assert node.id == "node1"
    # Unknown node -> None
    assert router.resolve_node("nodeX") is None


@pytest.mark.asyncio
async def test_router_round_robin(monkeypatch):
    router = AIRouter()
    # Three calls should cycle and wrap with 2 nodes
    a = router.resolve_node(None)
    b = router.resolve_node(None)
    c = router.resolve_node(None)
    assert a is not None and b is not None and c is not None
    assert a.id != b.id, "round-robin should advance to different node"
    assert c.id == a.id, "third selection should wrap to first"
    assert {a.id, b.id}.issubset({"node1", "node2"})


@pytest.mark.asyncio
async def test_router_fallback_on_failure(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    router = AIRouter(settings)

    # Mock providers: node1 fails, node2 succeeds
    class FakeProv:
        def __init__(self, should_fail=False):
            self.should_fail = should_fail
            self.default_timeout = 120

        async def chat_blocking(self, **kw):
            if self.should_fail:
                raise OllamaError("node1 down")
            return "ok from node2"

        async def chat(self, **kw):
            if self.should_fail:
                raise OllamaError("node1 down")
            yield "ok from node2"

    # Patch _get_provider to return fakes
    def fake_get_provider(node):
        return FakeProv(should_fail=(node.id == "node1"))

    monkeypatch.setattr(router, "_get_provider", fake_get_provider)

    monkeypatch.setattr(settings, "ai_fallback_enabled", True)

    result = await router.chat(messages=[{"role": "user", "content": "hi"}], requested_node="node1")
    assert result.content == "ok from node2"
    assert result.actual_node == "node2"


@pytest.mark.asyncio
async def test_router_no_fallback_raises(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    router = AIRouter(settings)

    class FailProv:
        default_timeout = 120

        async def chat_blocking(self, **kw):
            raise OllamaError("down")

        async def chat(self, **kw):
            raise OllamaError("down")
            yield  # noqa

    monkeypatch.setattr(router, "_get_provider", lambda node: FailProv())
    monkeypatch.setattr(settings, "ai_fallback_enabled", False)
    with pytest.raises(OllamaError):
        await router.chat(messages=[{"role": "user", "content": "hi"}], requested_node="node1")


@pytest.mark.asyncio
async def test_router_truncation():
    router = AIRouter()
    long = "x" * 10000
    msgs = [{"role": "user", "content": long}, {"role": "user", "content": long}]
    truncated = router._truncate_messages(msgs)
    # Should be within budget (8192*4=32768)
    total = sum(len(m["content"]) for m in truncated)
    assert total <= 32768
