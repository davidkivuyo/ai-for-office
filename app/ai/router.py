from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass

from fastapi import Request

from app.ai.health import HealthManager
from app.ai.models import NodeHealth
from app.ai.ollama import OllamaError, OllamaProvider
from app.config import OllamaNodeConfig, Settings, get_settings

logger = logging.getLogger(__name__)


class NodeSelectionError(ValueError):
    """Client-side node selection error that should map to HTTP 400."""

    pass


@dataclass
class RouteResult:
    content: str
    actual_node: str
    actual_model: str
    latency_ms: int


class AIRouter:
    """Request router — selects node, handles fallback, enforces concurrency policy.

    Per AGENTS.md §5 & §14:
    - explicit node selection
    - round-robin among healthy enabled nodes
    - fallback when selected node fails (configurable)
    - preserves requested vs actual model/node metadata
    - respects AI_MAX_CONCURRENT_REQUESTS_PER_NODE via per-node semaphore
    - does NOT shard or load-balance GPUs; simple routing only
    """

    def __init__(self, settings: Settings | None = None, health_manager: HealthManager | None = None) -> None:
        self._settings_obj: Settings | None = settings
        self._health_manager: HealthManager = health_manager or HealthManager()
        self._rr_cycle: itertools.cycle | None = None
        self._rr_ids: list[str] = []
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._semaphore_limits: dict[str, int] = {}
        self._providers: dict[str, OllamaProvider] = {}

    def _settings(self) -> Settings:
        if self._settings_obj is not None:
            return self._settings_obj
        return get_settings()

    def _nodes(self) -> list[OllamaNodeConfig]:
        return self._settings().ollama_nodes()

    def _get_provider(self, node: OllamaNodeConfig) -> OllamaProvider:
        if node.id not in self._providers:
            self._providers[node.id] = OllamaProvider(node.url, timeout=self._settings().ai_timeout_seconds)
        # update timeout if config changed
        self._providers[node.id].default_timeout = self._settings().ai_timeout_seconds
        return self._providers[node.id]

    def _get_semaphore(self, node_id: str) -> asyncio.Semaphore:
        max_conc = self._settings().ai_max_concurrent_requests_per_node
        if node_id not in self._semaphores or self._semaphore_limits.get(node_id) != max_conc:
            # recreate only when the configured limit changes; preserves existing semaphore while requests are active
            self._semaphores[node_id] = asyncio.Semaphore(max_conc)
            self._semaphore_limits[node_id] = max_conc
        return self._semaphores[node_id]

    def _default_node(self) -> OllamaNodeConfig | None:
        settings = self._settings()
        nid = settings.ai_default_node
        node = settings.get_node(nid)
        if node and node.enabled:
            return node
        # fallback to first enabled
        for n in self._nodes():
            if n.enabled:
                return n
        return None

    def _select_round_robin(self, exclude: set[str] | None = None) -> OllamaNodeConfig | None:
        enabled = [n for n in self._nodes() if n.enabled and (exclude is None or n.id not in exclude)]
        # Prefer healthy nodes if health info available
        hm = self._health_manager
        healthy = [n for n in enabled if hm.get(n.id).status == NodeHealth.healthy]
        pool = healthy if healthy else enabled
        if not pool:
            return None
        # Ensure cycle covers current pool
        pool_ids = [n.id for n in pool]
        if pool_ids != self._rr_ids:
            self._rr_ids = pool_ids
            self._rr_cycle = itertools.cycle(pool_ids)
        assert self._rr_cycle is not None
        chosen_id = next(self._rr_cycle)
        for n in pool:
            if n.id == chosen_id:
                return n
        return pool[0]

    def resolve_node(self, requested_node: str | None) -> OllamaNodeConfig | None:
        """Resolve which node to try first."""
        settings = self._settings()
        if requested_node:
            nid = requested_node.lower()
            node = settings.get_node(nid)
            if node is None:
                # Unknown node requested — treat as error, caller should 400
                return None
            if not node.enabled:
                return None
            return node
        # No explicit node — round-robin or default
        # For explicit testing we use round-robin if fallback enabled; otherwise default
        rr = self._select_round_robin()
        return rr or self._default_node()

    async def chat(
        self,
        *,
        messages: list[dict[str, str]],
        requested_node: str | None = None,
        stream: bool = False,
        **options,
    ) -> RouteResult:
        """Non-streaming entry used by chat API.

        Implements fallback per §14.
        """
        settings = self._settings()
        primary = self.resolve_node(requested_node)
        if primary is None:
            if requested_node is not None:
                node = settings.get_node(requested_node.lower())
                if node is None:
                    raise NodeSelectionError(f"Unknown node {requested_node!r}")
                if not node.enabled:
                    raise NodeSelectionError(f"Node {requested_node!r} is disabled")
            raise NodeSelectionError("No available nodes")

        tried: set[str] = set()
        candidates: list[OllamaNodeConfig] = [primary]
        if settings.ai_fallback_enabled:
            # Build fallback list — other enabled healthy nodes
            others = [n for n in self._nodes() if n.enabled and n.id != primary.id]
            # Prefer healthy
            hm = self._health_manager
            others_sorted = sorted(others, key=lambda n: 0 if hm.get(n.id).status == NodeHealth.healthy else 1)
            candidates.extend(others_sorted)

        last_err: Exception | None = None
        for node in candidates:
            if node.id in tried:
                continue
            tried.add(node.id)
            # Quick health gate: skip offline nodes unless it's the only candidate
            # Only skip if we've actually checked and found offline (last_checked not None)
            hm = self._health_manager
            hs = hm.get(node.id)
            if hs.status == NodeHealth.offline and hs.last_checked is not None and len(candidates) > 1 and node.id != primary.id:
                logger.info("router_skip_offline node=%s", node.id)
                continue
            if hs.status == NodeHealth.disabled:
                continue

            sem = self._get_semaphore(node.id)
            # Per-node concurrency: acquire semaphore with timeout? Use 0 wait to enforce single-concurrency
            # For Phase 1 default 1 concurrent — block briefly, then proceed
            async with sem:
                provider = self._get_provider(node)
                start = time.perf_counter()
                try:
                    # Enforce ai_max_context_tokens via model-specific provider truncation
                    truncated = self._truncate_messages(messages, model=node.model)
                    if stream:
                        # Collect stream for now — API layer handles SSE separately if needed
                        parts: list[str] = []
                        async for token in provider.chat(model=node.model, messages=truncated, stream=True, timeout=settings.ai_timeout_seconds, **options):
                            parts.append(token)
                        content = "".join(parts)
                    else:
                        content = await provider.chat_blocking(model=node.model, messages=truncated, timeout=settings.ai_timeout_seconds, **options)
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    logger.info("router_success node=%s model=%s latency_ms=%s", node.id, node.model, latency_ms)
                    return RouteResult(content=content, actual_node=node.id, actual_model=node.model, latency_ms=latency_ms)
                except OllamaError as e:
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    last_err = e
                    logger.warning("router_node_failed node=%s latency_ms=%s error=%s", node.id, latency_ms, e)
                    # mark degraded
                    hm.mark_failure(node.id, str(e))
                    if not settings.ai_fallback_enabled:
                        raise
                    # otherwise continue to fallback
                    continue

        # All candidates failed
        raise OllamaError(f"All nodes failed; last error: {last_err}") from last_err

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, str]],
        requested_node: str | None = None,
        **options,
    ):
        """Yield (token, metadata). For SSE we need to know final node/model."""
        # For simplicity, pick one node (with fallback) and stream from it.
        # We don't implement mid-stream fallback.
        settings = self._settings()
        primary = self.resolve_node(requested_node)
        if primary is None:
            if requested_node is not None:
                node = settings.get_node(requested_node.lower())
                if node is None:
                    raise NodeSelectionError(f"Unknown node {requested_node!r}")
                if not node.enabled:
                    raise NodeSelectionError(f"Node {requested_node!r} is disabled")
            raise NodeSelectionError("No available nodes")
        candidates: list[OllamaNodeConfig] = [primary]
        if settings.ai_fallback_enabled:
            others = [n for n in self._nodes() if n.enabled and n.id != primary.id]
            candidates.extend(others)

        last_err: Exception | None = None
        emitted = False
        for node in candidates:
            provider = self._get_provider(node)
            sem = self._get_semaphore(node.id)
            async with sem:
                try:
                    truncated = self._truncate_messages(messages, model=node.model)
                    async for token in provider.chat(model=node.model, messages=truncated, stream=True, timeout=settings.ai_timeout_seconds, **options):
                        emitted = True
                        yield token, node.id, node.model
                    return
                except OllamaError as e:
                    last_err = e
                    self._health_manager.mark_failure(node.id, str(e))
                    if emitted:
                        # Already streamed partial output — don't fallback to another node, surface controlled error
                        raise OllamaError(f"Stream interrupted after output started on {node.id}: {e}") from e
                    if not settings.ai_fallback_enabled:
                        raise
                    continue
        raise OllamaError(f"All nodes failed streaming; last error: {last_err}") from last_err

    def _truncate_messages(
        self, messages: list[dict[str, str]], model: str | None = None, max_context_tokens: int | None = None
    ) -> list[dict[str, str]]:
        """Conservative context truncation: delegate to provider for model-specific counting.

        Preserves newest user message while dropping older history.
        Falls back to char heuristic if provider not available.
        """
        settings = self._settings()
        budget_tokens = max_context_tokens if max_context_tokens is not None else settings.ai_max_context_tokens
        # Try model-specific provider truncation first
        try:
            # Resolve a provider for the given model or default node
            target_model = model
            if target_model is None:
                # Use default/primary node's model as representative
                node = self._default_node() or (self._nodes()[0] if self._nodes() else None)
                target_model = node.model if node else None
                provider = self._get_provider(node) if node else None
            else:
                # Find node matching model
                provider = None
                for n in self._nodes():
                    if n.model == target_model:
                        provider = self._get_provider(n)
                        break
                if provider is None and self._nodes():
                    provider = self._get_provider(self._nodes()[0])
            if provider is not None and hasattr(provider, "truncate_messages"):
                return provider.truncate_messages(messages, budget_tokens, model=target_model)  # type: ignore[no-untyped-call]
        except Exception:
            # Provider-specific truncation failed, fall back to character heuristic below
            pass
        # Fallback: char heuristic preserving newest
        budget_chars = budget_tokens * 4
        total = sum(len(m.get("content", "")) for m in messages)
        if total <= budget_chars:
            return messages
        system_msg: dict[str, str] | None = None
        rest = messages
        remaining_budget = budget_chars
        if messages and messages[0].get("role") == "system":
            system_msg = messages[0]
            rest = messages[1:]
            sys_len = len(system_msg.get("content", ""))
            if sys_len >= budget_chars:
                truncated_content = system_msg.get("content", "")[-budget_chars:] if budget_chars > 0 else ""
                if truncated_content:
                    return [{**system_msg, "content": truncated_content}]
                return []
            remaining_budget = budget_chars - sys_len

        kept: list[dict[str, str]] = []
        remaining = remaining_budget
        for m in reversed(rest):
            c_len = len(m.get("content", ""))
            if c_len <= remaining:
                kept.append(m)
                remaining -= c_len
            else:
                truncated_content = m.get("content", "")[-remaining:] if remaining > 0 else ""
                if truncated_content:
                    kept.append({**m, "content": truncated_content})
                break
        kept.reverse()
        if system_msg is not None:
            return [system_msg] + kept
        return kept

    def truncate_messages(
        self, messages: list[dict[str, str]], max_context_tokens: int | None = None
    ) -> list[dict[str, str]]:
        """Public API for API layer — enforces ai_max_context_tokens via provider delegation.

        Preserves newest user message when truncating older history.
        """
        return self._truncate_messages(messages, max_context_tokens=max_context_tokens)


def get_router(request: Request) -> AIRouter:
    """FastAPI dependency — returns the lifespan-scoped router from app.state."""
    return request.app.state.router  # type: ignore[no-redef,attr-defined]
