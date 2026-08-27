"""Focused HealthManager tests — covers healthy/degraded/offline/disabled/cooldown/concurrent."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.health import HealthManager
from app.ai.models import NodeHealth
from app.config import OllamaNodeConfig


def _fake_settings(nodes: list[OllamaNodeConfig]):
    """Return a mock settings with get_node / ollama_nodes."""
    mock = MagicMock()
    mock.get_node.side_effect = lambda nid: next((n for n in nodes if n.id == nid), None)
    mock.ollama_nodes.return_value = nodes
    return mock


def _node(node_id: str = "node1", url: str = "http://node1.test:11434", model: str = "qwen3:1.7b", enabled: bool = True) -> OllamaNodeConfig:
    return OllamaNodeConfig(node_id, url, model, enabled)


@pytest.mark.asyncio
async def test_check_node_healthy() -> None:
    hm = HealthManager(cooldown_seconds=30)
    fake = _fake_settings([_node()])
    healthy_result = {"status": "healthy", "models": ["qwen3:1.7b"], "url": "http://node1.test:11434"}

    with patch("app.ai.health.get_settings", return_value=fake), \
         patch("app.ai.health.OllamaProvider") as MockProv:
        MockProv.return_value.check_health = AsyncMock(return_value=healthy_result)
        before = time.time()
        st = await hm.check_node("node1")
        after = time.time()
        assert st.status == NodeHealth.healthy
        assert st.consecutive_failures == 0
        assert st.last_error is None
        assert st.last_success is not None and before <= st.last_success <= after
        assert st.last_checked is not None and before <= st.last_checked <= after
        assert st.detail == healthy_result
        assert st.model == "qwen3:1.7b"
        assert st.url == "http://node1.test:11434"
        # second call with different provider result should still be cached due to cooldown
        MockProv.return_value.check_health = AsyncMock(return_value={"status": "offline", "error": "boom"})
        st2 = await hm.check_node("node1")
        assert st2 is st  # same object, cached
        assert st2.status == NodeHealth.healthy  # not changed


@pytest.mark.asyncio
async def test_check_node_degraded() -> None:
    hm = HealthManager(cooldown_seconds=30)
    fake = _fake_settings([_node()])
    degraded = {"status": "degraded", "error": "model qwen3:1.7b not in tags", "models": []}

    with patch("app.ai.health.get_settings", return_value=fake), \
         patch("app.ai.health.OllamaProvider") as MockProv:
        MockProv.return_value.check_health = AsyncMock(return_value=degraded)
        st = await hm.check_node("node1")
        assert st.status == NodeHealth.degraded
        assert st.last_error == "model qwen3:1.7b not in tags"
        assert st.consecutive_failures == 0  # degraded does not increment failures
        assert st.last_checked is not None
        assert st.detail == degraded


@pytest.mark.asyncio
async def test_check_node_offline_progression() -> None:
    # Use cooldown 0 to allow repeated probes
    hm = HealthManager(cooldown_seconds=0)
    fake = _fake_settings([_node()])
    offline = {"status": "offline", "error": "connect failed"}

    with patch("app.ai.health.get_settings", return_value=fake), \
         patch("app.ai.health.OllamaProvider") as MockProv:
        MockProv.return_value.check_health = AsyncMock(return_value=offline)

        st1 = await hm.check_node("node1")
        assert st1.status == NodeHealth.degraded
        assert st1.consecutive_failures == 1
        assert st1.last_error == "connect failed"
        first_checked = st1.last_checked

        # ensure time progresses
        await asyncio.sleep(0.01)
        st2 = await hm.check_node("node1")
        assert st2.consecutive_failures == 2
        assert st2.status == NodeHealth.degraded
        assert st2.last_checked != first_checked

        await asyncio.sleep(0.01)
        st3 = await hm.check_node("node1")
        assert st3.consecutive_failures == 3
        assert st3.status == NodeHealth.offline
        assert st3.last_error == "connect failed"

        # healthy should reset failures
        MockProv.return_value.check_health = AsyncMock(return_value={"status": "healthy"})
        st4 = await hm.check_node("node1")
        assert st4.status == NodeHealth.healthy
        assert st4.consecutive_failures == 0
        assert st4.last_error is None
        assert st4.last_success is not None


@pytest.mark.asyncio
async def test_check_node_disabled() -> None:
    hm = HealthManager(cooldown_seconds=30)
    fake = _fake_settings([_node(enabled=False)])

    with patch("app.ai.health.get_settings", return_value=fake), \
         patch("app.ai.health.OllamaProvider") as MockProv:
        MockProv.return_value.check_health = AsyncMock()
        st = await hm.check_node("node1")
        assert st.status == NodeHealth.disabled
        assert st.last_checked is not None
        MockProv.return_value.check_health.assert_not_called()


@pytest.mark.asyncio
async def test_check_node_unknown() -> None:
    hm = HealthManager(cooldown_seconds=30)
    fake = _fake_settings([])
    fake.get_node.return_value = None

    with patch("app.ai.health.get_settings", return_value=fake):
        st = await hm.check_node("ghost")
        assert st.status == NodeHealth.offline
        assert st.last_error == "unknown node"


@pytest.mark.asyncio
async def test_check_node_cooldown_skipped() -> None:
    hm = HealthManager(cooldown_seconds=30)
    fake = _fake_settings([_node()])
    healthy = {"status": "healthy"}

    with patch("app.ai.health.get_settings", return_value=fake), \
         patch("app.ai.health.OllamaProvider") as MockProv:
        MockProv.return_value.check_health = AsyncMock(return_value=healthy)
        st1 = await hm.check_node("node1")
        assert st1.status == NodeHealth.healthy
        MockProv.return_value.check_health.reset_mock()
        # immediate second call — should be cached regardless of status
        st2 = await hm.check_node("node1")
        MockProv.return_value.check_health.assert_not_called()
        assert st2 is st1

        # degraded also cached
        hm2 = HealthManager(cooldown_seconds=30)
        fake2 = _fake_settings([_node()])
        degraded = {"status": "degraded", "error": "x"}
        with patch("app.ai.health.get_settings", return_value=fake2):
            with patch("app.ai.health.OllamaProvider") as MP2:
                MP2.return_value.check_health = AsyncMock(return_value=degraded)
                await hm2.check_node("node1")
                MP2.return_value.check_health.reset_mock()
                st_cached = await hm2.check_node("node1")
                MP2.return_value.check_health.assert_not_called()
                assert st_cached.status == NodeHealth.degraded


@pytest.mark.asyncio
async def test_check_node_concurrent_coalesced() -> None:
    hm = HealthManager(cooldown_seconds=30)
    fake = _fake_settings([_node()])
    call_count = 0

    async def fake_check(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return {"status": "healthy"}

    with patch("app.ai.health.get_settings", return_value=fake), \
         patch("app.ai.health.OllamaProvider") as MockProv:
        MockProv.return_value.check_health = AsyncMock(side_effect=fake_check)
        results = await asyncio.gather(*[hm.check_node("node1") for _ in range(5)])
        assert call_count == 1, f"concurrent probes should coalesce, got {call_count}"
        # all should be same healthy state, failure count preserved
        for r in results:
            assert r.status == NodeHealth.healthy
            assert r.consecutive_failures == 0
        # failure-count coalescing: offline increments only once
        hm2 = HealthManager(cooldown_seconds=0)
        fake2 = _fake_settings([_node()])
        call_count = 0

        async def fake_offline(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return {"status": "offline", "error": "boom"}

        with patch("app.ai.health.get_settings", return_value=fake2):
            with patch("app.ai.health.OllamaProvider") as MP2:
                MP2.return_value.check_health = AsyncMock(side_effect=fake_offline)
                results2 = await asyncio.gather(*[hm2.check_node("node1") for _ in range(3)])
                assert call_count == 1
                # all share same incremented count (1, not 3)
                assert results2[0].consecutive_failures == 1
                for r in results2:
                    assert r.consecutive_failures == 1


@pytest.mark.asyncio
async def test_check_all_preserves_state_under_lock() -> None:
    hm = HealthManager(cooldown_seconds=0)
    nodes = [_node("node1", "http://n1:11434", "m1"), _node("node2", "http://n2:11434", "m2")]
    fake = _fake_settings(nodes)

    async def fake_check(self, model, timeout=5.0):
        await asyncio.sleep(0.02)
        if model == "m1":
            return {"status": "healthy"}
        return {"status": "offline", "error": "fail"}

    with patch("app.ai.health.get_settings", return_value=fake), \
         patch("app.ai.health.OllamaProvider.check_health", new=fake_check):
        results = await hm.check_all()
        assert len(results) == 2
        by_id = {r.node_id: r for r in results}
        assert by_id["node1"].status == NodeHealth.healthy
        assert by_id["node2"].status == NodeHealth.degraded
        assert by_id["node2"].consecutive_failures == 1

        # concurrent check_all should coalesce per node
        call_counts: dict[str, int] = {"m1": 0, "m2": 0}

        async def counting_fake(self, model, timeout=5.0):
            call_counts[model] += 1
            await asyncio.sleep(0.03)
            return {"status": "healthy"}

        with patch("app.ai.health.OllamaProvider.check_health", new=counting_fake):
            hm3 = HealthManager(cooldown_seconds=30)
            fake3 = _fake_settings(nodes)
            with patch("app.ai.health.get_settings", return_value=fake3):
                # first prime
                await hm3.check_all()
                call_counts.clear()
                call_counts.update({"m1": 0, "m2": 0})
                # concurrent check_all when cache fresh → 0 calls (cached)
                await asyncio.gather(*[hm3.check_all() for _ in range(3)])
                assert call_counts["m1"] == 0 and call_counts["m2"] == 0

                # expire cache and concurrent
                for st in hm3.all():
                    st.last_checked = time.time() - 31
                call_counts.update({"m1": 0, "m2": 0})
                await asyncio.gather(*[hm3.check_all() for _ in range(3)])
                # each node probed once despite 3 concurrent check_all
                assert call_counts["m1"] == 1 and call_counts["m2"] == 1
