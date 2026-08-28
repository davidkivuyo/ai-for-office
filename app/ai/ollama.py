from __future__ import annotations


import json
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class OllamaProvider:
    """Ollama HTTP provider — thin wrapper, no business logic.

    Talks to a single Ollama node (base_url = http://host:11434).
    The router selects which provider instance to use.
    """

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout = timeout

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        stream: bool = True,
        timeout: float | None = None,
        **options,
    ) -> AsyncIterator[str]:
        effective_timeout = timeout if timeout is not None else self.default_timeout
        url = f"{self.base_url}/api/chat"
        payload: dict = {"model": model, "messages": messages, "stream": stream}
        # Ollama options
        ollama_opts: dict = {}
        if "temperature" in options and options["temperature"] is not None:
            ollama_opts["temperature"] = options["temperature"]
        if "num_predict" in options and options["num_predict"] is not None:
            ollama_opts["num_predict"] = options["num_predict"]
        if "num_ctx" in options and options["num_ctx"] is not None:
            ollama_opts["num_ctx"] = options["num_ctx"]
        # Disable Qwen3 extended thinking mode by default.
        # Qwen3 models output a <think>...</think> block before their answer which
        # consumes the token budget and returns empty content when the budget runs out.
        # think is a top-level Ollama field (not an options field).
        payload["think"] = options.get("think", False)
        # Allow caller to pass any extra ollama options via `options`
        if ollama_opts:
            payload["options"] = ollama_opts

        # Merge any remaining pass-through options at top level? Keep strict.
        # We intentionally do NOT forward arbitrary keys to avoid leaking business options.

        if stream:
            async for chunk in self._chat_stream(url, payload, effective_timeout):
                yield chunk
        else:
            text = await self._chat_blocking(url, payload, effective_timeout)
            yield text

    async def chat_blocking(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float | None = None,
        **options,
    ) -> str:
        chunks: list[str] = []
        async for c in self.chat(model=model, messages=messages, stream=False, timeout=timeout, **options):
            chunks.append(c)
        return "".join(chunks)

    # --- token counting / truncation (model-specific) ---
    def estimate_tokens(self, text: str, model: str | None = None) -> int:
        """Model-specific token estimate.

        For Qwen models keep heuristic ~4 chars/token; hook for future per-model
        tokenizers without leaking model specifics into API layer.
        """
        # Model-specific adjustment could be added here (e.g., different divisor per model)
        # For now keep conservative 4 chars/token for both qwen3:1.7b and qwen3.5:0.8b
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    def truncate_messages(
        self,
        messages: list[dict[str, str]],
        max_context_tokens: int,
        model: str | None = None,
    ) -> list[dict[str, str]]:
        """Truncate oldest history while preserving newest user message.

        Preserves leading system message and most-recent messages within
        max_context_tokens. Delegated from API layer to keep counting model-specific.
        """
        if max_context_tokens <= 0:
            return []
        # Compute budget via provider-owned estimate to stay model-aware
        # Use char heuristic via estimate_tokens to remain consistent
        total_tokens = sum(self.estimate_tokens(m.get("content", ""), model) for m in messages)
        if total_tokens <= max_context_tokens:
            return messages

        # Reserve system message if present
        system_msg: dict[str, str] | None = None
        rest = messages
        remaining = max_context_tokens
        if messages and messages[0].get("role") == "system":
            system_msg = messages[0]
            rest = messages[1:]
            sys_tokens = self.estimate_tokens(system_msg.get("content", ""), model)
            if sys_tokens >= max_context_tokens:
                # System alone exceeds budget — truncate its content to budget
                content = system_msg.get("content", "")
                # Approximate char budget from tokens
                char_budget = max_context_tokens * 4
                truncated = content[-char_budget:] if char_budget > 0 else ""
                if truncated:
                    return [{**system_msg, "content": truncated}]
                return []
            remaining = max_context_tokens - sys_tokens

        kept: list[dict[str, str]] = []
        # Walk newest first, preserving newest user message
        for m in reversed(rest):
            ct = self.estimate_tokens(m.get("content", ""), model)
            if ct <= remaining:
                kept.append(m)
                remaining -= ct
            else:
                # Truncate this oldest-kept message's content to fit remaining budget
                if remaining > 0:
                    char_budget = remaining * 4
                    content = m.get("content", "")
                    truncated = content[-char_budget:] if char_budget > 0 else ""
                    if truncated:
                        kept.append({**m, "content": truncated})
                break
        kept.reverse()
        if system_msg is not None:
            return [system_msg] + kept
        return kept

    # --- internals ---

    async def _chat_stream(self, url: str, payload: dict, timeout: float) -> AsyncIterator[str]:
        # httpx streaming
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            try:
                async with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise OllamaError(f"Ollama chat failed {resp.status_code}: {body.decode(errors='ignore')[:500]}")
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # Ollama streaming format: {"message": {"role":"assistant","content":"..."}, "done": false}
                        msg = data.get("message") or {}
                        content = msg.get("content")
                        if content:
                            yield content
                        if data.get("done"):
                            break
            except httpx.TimeoutException as e:
                raise OllamaError(f"Ollama timeout after {timeout}s: {e}") from e
            except httpx.ConnectError as e:
                raise OllamaError(f"Ollama connect error {self.base_url}: {e}") from e
            except httpx.HTTPError as e:
                raise OllamaError(f"Ollama transport error {self.base_url}: {e}") from e

    async def _chat_blocking(self, url: str, payload: dict, timeout: float) -> str:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    raise OllamaError(f"Ollama chat failed {resp.status_code}: {resp.text[:500]}")
                data = resp.json()
                # Non-streaming format: {"message": {"content": "..."}, "done": true}
                msg = data.get("message") or {}
                return msg.get("content", "")
            except httpx.TimeoutException as e:
                raise OllamaError(f"Ollama timeout after {timeout}s: {e}") from e
            except httpx.ConnectError as e:
                raise OllamaError(f"Ollama connect error {self.base_url}: {e}") from e
            except httpx.HTTPError as e:
                raise OllamaError(f"Ollama transport error {self.base_url}: {e}") from e

    async def check_health(self, model: str, timeout: float = 5.0) -> dict:
        """Health bundle: connectivity + api + model availability."""
        result: dict = {"url": self.base_url, "model": model}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3.0)) as client:
            # 1) tags endpoint reachable?
            try:
                resp = await client.get(f"{self.base_url}/api/tags")
                if resp.status_code != 200:
                    result.update({"status": "degraded", "error": f"/api/tags {resp.status_code}"})
                    return result
                tags = resp.json()
                models = [m.get("name", "") for m in tags.get("models", [])]
                # 2) model present?
                # Ollama tags may include variants like "qwen3:1.7b"; do exact or prefix match
                if any(model == m or m.startswith(model + ":") or model.startswith(m) for m in models):
                    result.update({"status": "healthy", "models": models})
                else:
                    result.update({"status": "degraded", "error": f"model {model!r} not in tags", "models": models})
                return result
            except Exception as e:
                result.update({"status": "offline", "error": str(e)[:500]})
                return result

    async def list_models(self, timeout: float = 5.0) -> list[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [m.get("name", "") for m in data.get("models", [])]
