import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import httpx

from app.ai.ollama import OllamaProvider, OllamaError


@pytest.mark.asyncio
async def test_ollama_chat_blocking_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaProvider("http://localhost:11434", timeout=5)

    # Mock httpx.AsyncClient.post
    async def fake_post(self: httpx.AsyncClient, url: str, json: dict[str, Any] | None = None) -> Any:  # noqa: A002
        class Resp:
            status_code: int = 200

            def json(self) -> dict[str, Any]:
                return {"message": {"role": "assistant", "content": "hello world"}, "done": True}

            @property
            def text(self) -> str:
                import json as _json

                return _json.dumps(self.json())

        return Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await provider.chat_blocking(model="qwen3:1.7b", messages=[{"role": "user", "content": "hi"}])
    assert result == "hello world"


@pytest.mark.asyncio
async def test_ollama_chat_stream_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaProvider("http://localhost:11434")

    # Mock streaming: patch _chat_stream to yield tokens without network
    async def fake_stream(url: str, payload: dict[str, Any], timeout: float) -> AsyncIterator[str]:
        for tok in ["hello", " ", "world"]:
            yield tok

    monkeypatch.setattr(provider, "_chat_stream", fake_stream)
    chunks: list[str] = []
    async for c in provider.chat(model="qwen3:1.7b", messages=[{"role": "user", "content": "hi"}], stream=True):
        chunks.append(c)
    assert "".join(chunks) == "hello world"


@pytest.mark.asyncio
async def test_ollama_error_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaProvider("http://localhost:11434")

    async def fake_post_err(self: httpx.AsyncClient, url: str, json: dict[str, Any] | None = None) -> Any:  # noqa: A002
        class Resp:
            status_code: int = 500
            text: str = "internal error"

            def json(self) -> dict[str, Any]:
                return {}

        return Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_err)
    with pytest.raises(OllamaError):
        await provider.chat_blocking(model="qwen3:1.7b", messages=[{"role": "user", "content": "hi"}])
