from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class AIProvider(Protocol):
    """Stable interface per AGENTS.md §12 — app code depends on this, not Ollama specifics."""

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        stream: bool = True,
        timeout: float | None = None,
        **options,
    ) -> AsyncIterator[str]:
        """Yield response tokens/chunks. Caller joins them for non-streaming."""

    async def chat_blocking(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float | None = None,
        **options,
    ) -> str:
        """Convenience helper — collect streamed chunks."""

    def estimate_tokens(self, text: str, model: str | None = None) -> int:
        """Model-specific token estimate — provider owns counting."""

    def truncate_messages(
        self,
        messages: list[dict[str, str]],
        max_context_tokens: int,
        model: str | None = None,
    ) -> list[dict[str, str]]:
        """Model-specific context truncation — preserves newest user message when dropping older history."""
