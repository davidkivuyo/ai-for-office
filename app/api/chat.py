from __future__ import annotations

import logging
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ollama import OllamaError
from app.ai.router import AIRouter, NodeSelectionError, get_router
from app.auth.permissions import get_current_user
from app.config import Settings, get_settings_dep
from app.db.models import User
from app.db.repositories import create_message, get_conversation, list_messages, create_conversation
from app.db.session import get_db
from app.files.prompts import build_chat_with_files_messages
from app.files.service import get_user_file, load_document_from_record, prepare_file_context
from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


def _derive_title(message: str) -> str:
    raw = message.strip().splitlines()[0].strip() if message.strip() else ""
    if not raw:
        return "Untitled"
    import re

    title = re.sub(r"\s+", " ", raw[:60].strip())
    return title or "Untitled"


def _is_generic_title(title: str) -> bool:
    t = (title or "").strip()
    return t in ("Untitled", "Untitled Document", "Untitled Document (unsaved)", "") or t.startswith("Untitled")


def _build_messages(history: list, new_content: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for m in history:
        # Only include user/assistant — ignore system for now
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": new_content})
    return messages


async def _load_file_docs(
    file_ids: list[str] | None,
    user_id: str,
    db: AsyncSession,
    question: str,
    settings: Settings,
) -> list:
    """Load and prepare file docs for chat — Phase 2A direct/chunked paths."""
    if not file_ids:
        return []
    docs = []
    for fid in file_ids:
        rec = await get_user_file(db, fid, user_id)
        if not rec:
            raise HTTPException(status_code=404, detail=f"File {fid} not found")
        doc = load_document_from_record(rec)
        docs.append(doc)
    # Apply size-based path (small direct vs medium chunked) per AGENTS §8/30
    return prepare_file_context(docs, question, settings=settings)


def _build_messages_with_files(
    history: list,
    question: str,
    file_docs: list,
) -> list[dict[str, str]]:
    """Build messages including file context when present."""
    if not file_docs:
        return _build_messages(history, question)
    # Convert history SQLAlchemy objects to dicts for prompt builder
    hist_dicts = [{"role": m.role, "content": m.content} for m in history if m.role in ("user", "assistant")]
    return build_chat_with_files_messages(hist_dicts, question, file_docs)


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
        if _is_generic_title(conversation.title):
            new_title = _derive_title(payload.message)
            if new_title and new_title != conversation.title:
                conversation.title = new_title
                from datetime import datetime, timezone

                conversation.updated_at = datetime.now(timezone.utc)
                await db.flush()
    else:
        # Auto-create with title from first message prefix (context-aware)
        title = _derive_title(payload.message)
        conversation = await create_conversation(db, current.id, title=title)
        await db.flush()

    # Phase 2A: file context — load and prepare docs (small direct vs medium chunked) before persisting message so filenames can be stored
    file_docs = await _load_file_docs(payload.file_ids, current.id, db, payload.message, settings)
    attached_filenames = [d.filename for d in file_docs] if file_docs else None

    # Persist user message first (with attached filenames for reload rendering)
    await create_message(db, conversation.id, role="user", content=payload.message, files=attached_filenames)
    await db.flush()

    # Build history for model
    history = await list_messages(db, conversation.id)
    # The history already includes the just-persisted user message as last element; exclude it for model input
    if history and history[-1].role == "user" and history[-1].content == payload.message:
        history_for_model = history[:-1]
    else:
        history_for_model = history

    messages = _build_messages_with_files(history_for_model, payload.message, file_docs)
    # Enforce ai_max_context_tokens before inference via provider layer (preserves newest user message)
    # Delegates counting/truncation to AI provider to keep it model-specific
    messages = ai_router.truncate_messages(messages, max_context_tokens=settings.ai_max_context_tokens)

    # Enforce output/context limits via router options (per AGENTS §9)
    options: dict = {}
    if payload.temperature is not None:
        options["temperature"] = payload.temperature
    options["num_predict"] = settings.ai_max_output_tokens
    options["num_ctx"] = settings.ai_num_ctx

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

    try:
        result = await ai_router.chat(messages=messages, requested_node=requested_node, stream=False, **options)
    except NodeSelectionError as e:
        await db.commit()  # commit user message so history preserved
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OllamaError as e:
        _safe_node = requested_node.replace("\n", "_").replace("\r", "_") if isinstance(requested_node, str) else requested_node
        _safe_error = str(e).replace("\n", "_").replace("\r", "_")
        logger.warning("chat_failed request_id=%s user=%s conv=%s node=%s error=%s", request_id, current.id, conversation.id, _safe_node, _safe_error)
        # Do not persist assistant message on failure — return controlled error
        await db.commit()  # commit user message so history preserved
        raise HTTPException(status_code=502, detail="Inference failed") from e

    latency_ms = result.latency_ms

    # Audit-friendly metadata per AGENTS §14
    _safe_requested_node_info = requested_node.replace("\n", "_").replace("\r", "_") if isinstance(requested_node, str) else requested_node
    logger.info(
        "chat_success request_id=%s user_id=%s conversation_id=%s requested_node=%s actual_node=%s requested_model=%s actual_model=%s latency_ms=%s",
        request_id,
        current.id,
        conversation.id,
        _safe_requested_node_info,
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
        if _is_generic_title(conversation.title):
            new_title = _derive_title(payload.message)
            if new_title and new_title != conversation.title:
                conversation.title = new_title
                from datetime import datetime, timezone

                conversation.updated_at = datetime.now(timezone.utc)
                await db.flush()
    else:
        conversation = await create_conversation(db, current.id, title=_derive_title(payload.message))
        await db.flush()

    # Phase 2A: file context for streaming — load before persisting so filenames can be stored
    file_docs = await _load_file_docs(payload.file_ids, current.id, db, payload.message, settings)
    attached_filenames = [d.filename for d in file_docs] if file_docs else None
    await create_message(db, conversation.id, role="user", content=payload.message, files=attached_filenames)
    await db.flush()
    history = await list_messages(db, conversation.id)
    # Exclude just-persisted user msg for model input
    history_for_model = history[:-1] if history and history[-1].role == "user" else history
    messages = _build_messages_with_files(history_for_model, payload.message, file_docs)
    # Enforce ai_max_context_tokens before inference via provider layer (preserves newest user message)
    messages = ai_router.truncate_messages(messages, max_context_tokens=settings.ai_max_context_tokens)
    options: dict = {}
    if payload.temperature is not None:
        options["temperature"] = payload.temperature
    options["num_predict"] = settings.ai_max_output_tokens
    options["num_ctx"] = settings.ai_num_ctx

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
            _safe_node = requested_node.replace("\n", "_").replace("\r", "_") if isinstance(requested_node, str) else requested_node
            _safe_error = str(e).replace("\n", "_").replace("\r", "_")
            logger.warning("chat_stream_failed conv=%s node=%s error=%s", conv_id, _safe_node, _safe_error)
            import json

            yield f"data: {json.dumps({'error': 'Node selection failed'})}\n\n"
            yield "data: [DONE]\n\n"
            return
        except OllamaError as e:
            _safe_node2 = requested_node.replace("\n", "_").replace("\r", "_") if isinstance(requested_node, str) else requested_node
            _safe_error2 = str(e).replace("\n", "_").replace("\r", "_")
            logger.warning("chat_stream_failed conv=%s node=%s error=%s", conv_id, _safe_node2, _safe_error2)
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
