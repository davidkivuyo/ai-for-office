from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    conversation_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        description="Existing conversation id or None to create one",
    )
    message: str = Field(min_length=1, max_length=8192)

    @field_validator("message")
    @classmethod
    def _validate_message(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must contain non-whitespace characters")
        return v
    node_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=32,
        pattern=r"(?i)^node[a-zA-Z0-9_-]*$",
        description="Explicit node id e.g. node1; if omitted router decides",
    )
    stream: bool = Field(default=False)
    temperature: float | None = Field(default=None, ge=0, le=2)


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str
    message_id: str
    requested_node: str | None = None
    actual_node: str
    requested_model: str | None = None
    actual_model: str
    latency_ms: int


class ChatStreamChunk(BaseModel):
    token: str
    done: bool = False
