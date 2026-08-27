from __future__ import annotations

import logging
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ollama import OllamaError
from app.ai.router import AIRouter, NodeSelectionError, get_router
from app.auth.permissions import get_current_user
from app.config import Settings, get_settings_dep
from app.db.models import User
from app.db.repositories import create_message, get_conversation, list_messages, create_conversation
from app.db.session import get_db
from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


def _build_messages(history: list, new_content: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for m in history:
        # Only include user/assistant — ignore system for now
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": new_content})
    return messages


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ai_router: AIRouter = Depends(get_router),
    settings: Settings = Depends(get_settings_dep),
):
    request_id = str(uuid.uuid4())[:8]

    # Validate requested node if provided — distinguish unknown/disabled
    requested_node = payload.node_id.lower() if payload.node_id else None
    if requested_node:
        node = settings.get_node(requested_node)
        if node is None:
            raise HTTPException(status_code=400, detail=f"Unknown node {payload.node_id!r}")
        if not node.enabled:
            raise HTTPException(status_code=400, detail=f"Node {payload.node_id!r} is disabled")
    elif not any(n.enabled for n in settings.ollama_nodes()):
        raise HTTPException(status_code=400, detail="No available nodes")

    # Resolve or create conversation
    conversation = None
    if payload.conversation_id:
        conversation = await get_conversation(db, payload.conversation_id, current.id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        # Auto-create with title from first message prefix
        title = payload.message[:60] or "Untitled"
        conversation = await create_conversation(db, current.id, title=title)
        await db.flush()

    # Persist user message first
    await create_message(db, conversation.id, role="user", content=payload.message)
    await db.flush()

    # Build history for model
    history = await list_messages(db, conversation.id)
    messages = _build_messages([m for m in history if m.role != "user" or m.content != payload.message or True], payload.message)
    # The above already includes the new message; avoid duplication — rebuild correctly:
    # Instead use history without the just-inserted duplicate? We add all history then append new, but history already contains new.
    # Simpler: use history[:-1] + new
    # history includes the persisted user message as last element; so exclude it then append.
    if history and history[-1].role == "user" and history[-1].content == payload.message:
        history_for_model = history[:-1]
    else:
        history_for_model = history
    messages = _build_messages(history_for_model, payload.message)
    # Enforce ai_max_context_tokens before inference via provider layer (preserves newest user message)
    # Delegates counting/truncation to AI provider to keep it model-specific
    messages = ai_router.truncate_messages(messages, max_context_tokens=settings.ai_max_context_tokens)

    # Enforce max output tokens via router options (retain existing handling)
    options: dict = {}
    if payload.temperature is not None:
        options["temperature"] = payload.temperature
    options["num_predict"] = settings.ai_max_output_tokens

    # Determine requested model for audit
    req_model = None
    if requested_node:
        node = settings.get_node(requested_node)
        req_model = node.model if node else None
    else:
        # default / round-robin — we record requested as None; actual will be whatever router picks
        pass

    # Streaming via SSE if requested
    if payload.stream:
        # For SSE we return a streaming response that also persists the assistant message at the end.
        # To avoid holding DB tx open during streaming, we stream directly and then background-persist.
        # For Phase 1 we implement a simpler streaming endpoint at /api/chat/stream.

        # Fall through to non-streaming error — tell client to use /api/chat/stream
        raise HTTPException(status_code=400, detail="Use POST /api/chat/stream for streaming")

    start = time.perf_counter()
    try:
        result = await ai_router.chat(messages=messages, requested_node=requested_node, stream=False, **options)
    except NodeSelectionError as e:
        await db.commit()  # commit user message so history preserved
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OllamaError as e:
        logger.warning("chat_failed request_id=%s user=%s conv=%s node=%s error=%s", request_id, current.id, conversation.id, requested_node, e)
        # Do not persist assistant message on failure — return controlled error
        await db.commit()  # commit user message so history preserved
        raise HTTPException(status_code=502, detail="Inference failed") from e

    latency_ms = result.latency_ms

    # Audit-friendly metadata per AGENTS §14
    logger.info(
        "chat_success request_id=%s user_id=%s conversation_id=%s requested_node=%s actual_node=%s requested_model=%s actual_model=%s latency_ms=%s",
        request_id,
        current.id,
        conversation.id,
        requested_node,
        result.actual_node,
        req_model,
        result.actual_model,
        latency_ms,
    )

    # Persist assistant message
    assistant_msg = await create_message(
        db,
        conversation.id,
        role="assistant",
        content=result.content,
        model=result.actual_model,
        node_id=result.actual_node,
        latency_ms=latency_ms,
    )
    # Update conversation timestamp
    conversation.title = conversation.title  # touch updated_at via onupdate? ensure update
    # Manually bump updated_at
    from datetime import datetime, timezone

    conversation.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(assistant_msg)

    return ChatResponse(
        reply=result.content,
        conversation_id=conversation.id,
        message_id=assistant_msg.id,
        requested_node=requested_node,
        actual_node=result.actual_node,
        requested_model=req_model,
        actual_model=result.actual_model,
        latency_ms=latency_ms,
    )


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ai_router: AIRouter = Depends(get_router),
    settings: Settings = Depends(get_settings_dep),
):
    """SSE streaming — emits `data: {"token": "..."}` lines, ends with `data: [DONE]`.

    Persists both messages after stream completes.
    """
    if payload.node_id:
        _node = settings.get_node(payload.node_id.lower())
        if _node is None:
            raise HTTPException(status_code=400, detail=f"Unknown node {payload.node_id!r}")
        if not _node.enabled:
            raise HTTPException(status_code=400, detail=f"Node {payload.node_id!r} is disabled")

    requested_node = payload.node_id.lower() if payload.node_id else None
    if requested_node is None and not any(n.enabled for n in settings.ollama_nodes()):
        raise HTTPException(status_code=400, detail="No available nodes")

    # Resolve/create conversation
    if payload.conversation_id:
        conversation = await get_conversation(db, payload.conversation_id, current.id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        conversation = await create_conversation(db, current.id, title=payload.message[:60] or "Untitled")
        await db.flush()

    await create_message(db, conversation.id, role="user", content=payload.message)
    await db.flush()
    history = await list_messages(db, conversation.id)
    # Exclude just-persisted user msg for model input
    history_for_model = history[:-1] if history and history[-1].role == "user" else history
    messages = _build_messages(history_for_model, payload.message)
    # Enforce ai_max_context_tokens before inference via provider layer (preserves newest user message)
    messages = ai_router.truncate_messages(messages, max_context_tokens=settings.ai_max_context_tokens)
    options: dict = {}
    if payload.temperature is not None:
        options["temperature"] = payload.temperature
    options["num_predict"] = settings.ai_max_output_tokens

    # Need conversation_id for closure
    conv_id = conversation.id
    user_id = current.id

    # Commit user message before streaming so it persists even if client disconnects;
    # capture engine for a dedicated stream-scoped session (valid for entire event_gen lifecycle)
    await db.commit()
    stream_engine = getattr(db, "bind", None)
    if stream_engine is None:
        try:
            stream_engine = db.get_bind()  # type: ignore[no-untyped-call]
        except Exception:
            from app.db.session import get_engine

            stream_engine = get_engine()

    async def event_gen() -> AsyncIterator[str]:
        full: list[str] = []
        actual_node = requested_node or settings.ai_default_node
        actual_model = ""
        start = time.perf_counter()
        try:
            async for token, node_id, model in ai_router.chat_stream(messages=messages, requested_node=requested_node, **options):
                full.append(token)
                actual_node = node_id
                actual_model = model
                # SSE format
                import json

                yield f"data: {json.dumps({'token': token, 'node_id': node_id, 'model': model}, ensure_ascii=False)}\n\n"
        except NodeSelectionError as e:
            logger.warning("chat_stream_failed conv=%s node=%s error=%s", conv_id, requested_node, e)
            import json

            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"
            return
        except OllamaError as e:
            logger.warning("chat_stream_failed conv=%s node=%s error=%s", conv_id, requested_node, e)
            import json

            yield f"data: {json.dumps({'error': 'Inference failed'})}\n\n"
            yield "data: [DONE]\n\n"
            return
        # Persist assistant reply after stream using dedicated stream-scoped session
        content = "".join(full)
        latency_ms = int((time.perf_counter() - start) * 1000)
        latency_ms = max(latency_ms, 0)
        from sqlalchemy.ext.asyncio import async_sessionmaker as _sessionmaker

        _factory = _sessionmaker(stream_engine, expire_on_commit=False, class_=AsyncSession)
        try:
            async with _factory() as stream_db:
                await create_message(stream_db, conv_id, role="assistant", content=content, model=actual_model, node_id=actual_node, latency_ms=latency_ms)
                # bump updated_at
                from datetime import datetime, timezone

                conv = await get_conversation(stream_db, conv_id, user_id)
                if conv is not None:
                    conv.updated_at = datetime.now(timezone.utc)
                await stream_db.commit()
        except Exception as e:
            logger.exception("chat_stream_persist_failed conv=%s error=%s", conv_id, e)
            import json

            yield f"data: {json.dumps({'error': 'Persistence failed'})}\n\n"
            yield "data: [DONE]\n\n"
            return
        import json

        yield f"data: {json.dumps({'done': True, 'conversation_id': conv_id, 'actual_node': actual_node, 'actual_model': actual_model, 'latency_ms': latency_ms})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
