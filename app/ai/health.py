from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from app.ai.models import NodeHealth
from app.ai.ollama import OllamaProvider
from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class NodeHealthState:
    node_id: str
    status: NodeHealth = NodeHealth.offline
    last_checked: float | None = None
    last_success: float | None = None
    last_error: str | None = None
    model: str = ""
    url: str = ""
    consecutive_failures: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


class HealthManager:
    """Keeps per-node health, avoids tight retry loops via cooldown."""

    def __init__(self, cooldown_seconds: float = 30.0) -> None:
        self._states: dict[str, NodeHealthState] = {}
        self.cooldown = cooldown_seconds
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[Any]] = {}

    def get(self, node_id: str) -> NodeHealthState:
        if node_id not in self._states:
            # lazy init from config if possible
            settings = get_settings()
            node = settings.get_node(node_id)
            if node:
                st = NodeHealthState(node_id=node_id, model=node.model, url=node.url)
                if not node.enabled:
                    st.status = NodeHealth.disabled
                self._states[node_id] = st
            else:
                self._states[node_id] = NodeHealthState(node_id=node_id)
        return self._states[node_id]

    def all(self) -> list[NodeHealthState]:
        settings = get_settings()
        for n in settings.ollama_nodes():
            self.get(n.id)
        return [self._states[n.id] for n in settings.ollama_nodes()]

    def mark_success(self, node_id: str) -> None:
        st = self.get(node_id)
        st.status = NodeHealth.healthy
        st.last_success = time.time()
        st.last_checked = time.time()
        st.last_error = None
        st.consecutive_failures = 0

    def mark_failure(self, node_id: str, error: str) -> None:
        st = self.get(node_id)
        st.consecutive_failures += 1
        st.last_error = error[:500]
        st.last_checked = time.time()
        if st.consecutive_failures >= 3:
            st.status = NodeHealth.offline
        elif st.consecutive_failures >= 1:
            st.status = NodeHealth.degraded
        logger.warning("health_mark_failure node=%s failures=%s error=%s", node_id, st.consecutive_failures, error[:200])

    def mark_disabled(self, node_id: str) -> None:
        st = self.get(node_id)
        st.status = NodeHealth.disabled

    async def check_node(self, node_id: str) -> NodeHealthState:
        settings = get_settings()
        node = settings.get_node(node_id)
        if node is None:
            async with self._lock:
                st = self.get(node_id)
                st.status = NodeHealth.offline
                st.last_error = "unknown node"
                return st
        if not node.enabled:
            async with self._lock:
                st = self.get(node_id)
                st.status = NodeHealth.disabled
                st.last_checked = time.time()
                return st

        # Cache recently fresh states regardless of status and coalesce in-flight checks
        fut_to_await: asyncio.Future[Any] | None = None
        async with self._lock:
            st = self.get(node_id)
            if st.last_checked and (time.time() - st.last_checked) < self.cooldown:
                return st
            if node_id in self._inflight:
                fut_to_await = self._inflight[node_id]
            else:
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[Any] = loop.create_future()
                self._inflight[node_id] = fut
        if fut_to_await is not None:
            try:
                await fut_to_await
            except Exception:
                pass
            async with self._lock:
                return self.get(node_id)

        # Owner: perform provider probe outside lock
        provider = OllamaProvider(node.url, timeout=5.0)
        try:
            result = await provider.check_health(node.model, timeout=5.0)
        except asyncio.CancelledError:
            async with self._lock:
                fut = self._inflight.pop(node_id, None)
                if fut is not None and not fut.done():
                    fut.cancel()
            raise
        except Exception as e:  # pragma: no cover — provider normally returns offline dict
            result = {"status": "offline", "error": str(e)[:500]}

        async with self._lock:
            st = self.get(node_id)
            st.last_checked = time.time()
            st.model = node.model
            st.url = node.url
            st.detail = result
            status = result.get("status")
            if status == "healthy":
                st.status = NodeHealth.healthy
                st.last_success = time.time()
                st.last_checked = time.time()
                st.last_error = None
                st.consecutive_failures = 0
            elif status == "degraded":
                st.status = NodeHealth.degraded
                st.last_error = result.get("error")
            else:
                # Increment under lock — preserves concurrent increments, no last-writer-wins
                st.consecutive_failures += 1
                st.last_error = result.get("error", "offline")[:500]
                st.last_checked = time.time()
                if st.consecutive_failures >= 3:
                    st.status = NodeHealth.offline
                elif st.consecutive_failures >= 1:
                    st.status = NodeHealth.degraded
                logger.warning(
                    "health_mark_failure node=%s failures=%s error=%s",
                    node_id,
                    st.consecutive_failures,
                    result.get("error", "offline")[:200],
                )
            fut = self._inflight.pop(node_id, None)
            if fut is not None and not fut.done():
                fut.set_result(st)
            return st

    async def check_all(self) -> list[NodeHealthState]:
        settings = get_settings()
        nodes = settings.ollama_nodes()
        results = await asyncio.gather(*(self.check_node(n.id) for n in nodes))
        return list(results)

    def reset(self) -> None:
        self._states.clear()
        # Cancel and clear any in-flight probes to avoid stale futures
        for fut in list(self._inflight.values()):
            if not fut.done():
                fut.cancel()
        self._inflight.clear()


def get_health_manager_dep(request: Request) -> HealthManager:
    """FastAPI dependency — returns lifespan-scoped HealthManager from app.state."""
    return request.app.state.health_manager  # type: ignore[attr-defined]
