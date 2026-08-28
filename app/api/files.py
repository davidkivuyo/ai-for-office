from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import get_current_user
from app.config import Settings, get_settings_dep
from app.db.models import User
from app.db.repositories import get_conversation
from app.db.session import get_db
from app.files.service import handle_file_upload, get_user_file, list_user_files, load_document_from_record

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    conversation_id: Optional[str] = Form(default=None),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """Upload and extract file per AGENTS §3-9.

    - Validates type and size
    - Extracts via Python (never LLM)
    - Estimates tokens + categorizes
    - Persists normalized content for chat
    - Returns metadata + preview for UI
    """
    # Validate filename
    filename = file.filename or "untitled"
    # Bounded read per finding: consume in chunks and enforce size limit early
    max_size = settings.file_max_size_bytes if settings and hasattr(settings, "file_max_size_bytes") else 10 * 1024 * 1024
    chunk_size = 1024 * 1024  # 1 MiB
    data_parts: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise HTTPException(status_code=413, detail=f"File too large ({total} bytes). Max {max_size} bytes.")
            data_parts.append(chunk)
        data = b"".join(data_parts)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("file_read_failed filename=%s error=%s", filename, e)
        raise HTTPException(status_code=400, detail="Failed to read upload")

    # Conversation_id validation if provided
    conv_id = conversation_id.strip() if conversation_id and conversation_id.strip() else None
    if conv_id:
        import re

        if not re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", conv_id):
            raise HTTPException(status_code=400, detail="Invalid conversation_id")
        conv = await get_conversation(db, conv_id, current.id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

    try:
        record, doc = await handle_file_upload(
            file_bytes=data,
            filename=filename,
            user_id=current.id,
            conversation_id=conv_id,
            db=db,
            settings=settings,
        )
        await db.commit()
        await db.refresh(record)
    except ValueError as e:
        msg = str(e)
        # Unsupported type -> 400 with clear message per §4
        if "Unsupported file type" in msg:
            raise HTTPException(status_code=400, detail=msg)
        if "File too large" in msg or "Empty file" in msg:
            raise HTTPException(status_code=413 if "too large" in msg else 400, detail=msg)
        if "Failed to extract" in msg:
            raise HTTPException(status_code=422, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as e:
        logger.exception("file_upload_failed user=%s filename=%s error=%s", current.id, filename, e)
        raise HTTPException(status_code=500, detail="File processing failed")

    # Build preview (truncate for UI)
    preview_chars = settings.file_max_text_preview_chars if settings else 1200
    preview = doc.text[:preview_chars] + ("…[truncated]" if len(doc.text) > preview_chars else "")
    # For tables, include first rows preview
    table_preview = None
    if doc.tables:
        t = doc.tables[0]
        table_preview = {
            "name": t.name,
            "headers": t.headers[:10],
            "rows": [row[:10] for row in t.rows[:3]],
            "row_count": len(t.rows),
            "source": t.source,
        }

    return {
        "file_id": record.id,
        "filename": record.filename,
        "file_type": record.file_type,
        "token_estimate": record.token_estimate,
        "size_category": record.size_category,
        "pages": record.pages,
        "sheets": doc.sheets,
        "tables_count": len(doc.tables),
        "preview": preview,
        "table_preview": table_preview,
        "metadata": doc.metadata,
        "created_at": record.created_at.isoformat(),
    }


@router.get("/{file_id}")
async def get_file(
    file_id: str,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rec = await get_user_file(db, file_id, current.id)
    if not rec:
        raise HTTPException(status_code=404, detail="File not found")
    doc = load_document_from_record(rec)
    return {
        "file_id": rec.id,
        "filename": rec.filename,
        "file_type": rec.file_type,
        "token_estimate": rec.token_estimate,
        "size_category": rec.size_category,
        "pages": rec.pages,
        "content_text": rec.content_text,
        "content": doc.to_dict(),
        "created_at": rec.created_at.isoformat(),
    }


@router.get("")
async def list_files(
    conversation_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    files = await list_user_files(db, current.id, conversation_id=conversation_id, limit=limit)
    return [
        {
            "file_id": f.id,
            "filename": f.filename,
            "file_type": f.file_type,
            "token_estimate": f.token_estimate,
            "size_category": f.size_category,
            "pages": f.pages,
            "conversation_id": f.conversation_id,
            "byte_size": f.byte_size,
            "created_at": f.created_at.isoformat(),
        }
        for f in files
    ]


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    file_id: str,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rec = await get_user_file(db, file_id, current.id)
    if not rec:
        raise HTTPException(status_code=404, detail="File not found")
    await db.delete(rec)
    await db.commit()
    return None
