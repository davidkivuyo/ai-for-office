from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import Settings
from app.db.models import UploadedFile
from app.files.chunking import select_relevant_chunks, chunk_document
from app.files.extractor import extract_document, detect_file_type, SUPPORTED_TYPES_STR
from app.files.models import DocumentContent
from app.files.prompts import build_file_messages, build_chat_with_files_messages, format_document_for_llm


# Max file size guard
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


async def handle_file_upload(
    *,
    file_bytes: bytes,
    filename: str,
    user_id: str,
    conversation_id: str | None,
    db: AsyncSession,
    settings: Settings | None = None,
) -> tuple[UploadedFile, DocumentContent]:
    """Validate, extract, classify, persist upload.

    - Validates size and extension (untrusted input)
    - Extracts via Python (never LLM)
    - Estimates tokens and categorizes size
    - Persists normalized content for later chat use
    """
    # Validate filename presence
    if not filename or not filename.strip():
        raise ValueError("Filename is required")
    filename = Path(filename).name.strip()  # strip path traversal
    if not filename:
        raise ValueError("Invalid filename")

    # Extension check
    try:
        file_type = detect_file_type(filename)
    except ValueError as e:
        raise ValueError(str(e)) from e

    # Size check
    max_bytes = settings.file_max_size_bytes if settings else DEFAULT_MAX_BYTES
    if len(file_bytes) > max_bytes:
        raise ValueError(f"File too large ({len(file_bytes)} bytes). Max {max_bytes} bytes.")
    if len(file_bytes) == 0:
        raise ValueError("Empty file")

    # Extraction (untrusted bytes -> normalized)
    try:
        doc = extract_document(file_bytes, filename)
    except ValueError:
        raise
    except RuntimeError as e:
        raise ValueError(str(e)) from e
    except Exception as e:
        # Wrap unexpected extraction errors safely (don't leak internals)
        raise ValueError(f"Failed to extract {filename!r}: {e}") from e

    # Persist
    content_json = json.dumps(doc.to_dict(), ensure_ascii=False)
    uploaded = UploadedFile(
        user_id=user_id,
        conversation_id=conversation_id,
        filename=filename,
        file_type=file_type,
        content_text=doc.text,
        content_json=content_json,
        token_estimate=doc.token_estimate,
        size_category=doc.size_category,
        pages=doc.pages,
        byte_size=len(file_bytes),
    )
    db.add(uploaded)
    await db.flush()  # get id
    return uploaded, doc


def load_document_from_record(record: UploadedFile) -> DocumentContent:
    """Deserialize stored JSON back to DocumentContent."""
    data = json.loads(record.content_json)
    return DocumentContent.from_dict(data)


async def get_user_file(db: AsyncSession, file_id: str, user_id: str) -> UploadedFile | None:
    result = await db.execute(select(UploadedFile).where(UploadedFile.id == file_id, UploadedFile.user_id == user_id))
    return result.scalars().first()


async def list_user_files(db: AsyncSession, user_id: str, conversation_id: str | None = None, limit: int = 20) -> list[UploadedFile]:
    q = select(UploadedFile).where(UploadedFile.user_id == user_id).order_by(UploadedFile.created_at.desc()).limit(limit)
    if conversation_id:
        q = select(UploadedFile).where(UploadedFile.user_id == user_id, UploadedFile.conversation_id == conversation_id).order_by(UploadedFile.created_at.desc()).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


def prepare_file_context(
    docs: list[DocumentContent],
    question: str,
    *,
    settings: Settings | None = None,
) -> list[DocumentContent]:
    """Apply size check and chunking per AGENTS §8 and §30.

    - small (<4k tokens): return full doc (direct LLM context)
    - medium (4k-12k): return relevant chunks as filtered DocumentContent with sliced tables/text
    - large (>12k): same as medium but with chunk/retrieval hint (for Phase 2A, treat as medium)
    """
    if not docs:
        return []

    small_thresh = settings.file_small_token_threshold if settings else 4000
    medium_thresh = settings.file_medium_token_threshold if settings else 12000

    prepared: list[DocumentContent] = []
    for doc in docs:
        if doc.token_estimate < small_thresh:
            # Fast path: direct context (no chunking) — routing uses token estimate only (§8)
            prepared.append(doc)
        elif doc.token_estimate <= medium_thresh:
            # Structured chunking: retrieve only relevant sheets/rows/pages
            relevant = select_relevant_chunks(doc, question, max_chunks=3)
            if not relevant:
                prepared.append(doc)
                continue
            # Build a filtered DocumentContent that contains only relevant chunk text/tables
            # Preserve original metadata but replace text/tables with chunked subset
            filtered_tables = []
            filtered_text_parts = []
            chunk_sources = []
            for ch in relevant:
                if ch.table_slice:
                    filtered_tables.append(ch.table_slice)
                if ch.text:
                    filtered_text_parts.append(ch.text)
                if ch.source:
                    chunk_sources.append(ch.source)
            # Derive retained sections from chunk citation metadata (§29)
            chunk_source_set = set(chunk_sources)
            retained_sections = [
                s
                for s in doc.sections
                if s.source in chunk_source_set
                or s.title in chunk_source_set
                or f"Section: {s.title}" in chunk_source_set
                or any(s.title in src or (s.source and s.source in src) for src in chunk_source_set)
            ]
            # Build filtered doc
            filtered = DocumentContent(
                filename=doc.filename,
                file_type=doc.file_type,
                text="\n\n".join(filtered_text_parts) if filtered_text_parts else doc.text,
                tables=filtered_tables if filtered_tables else doc.tables,
                sheets=doc.sheets,
                pages=doc.pages,
                sections=retained_sections,
                metadata={**doc.metadata, "chunked": True, "chunk_sources": chunk_sources, "original_tokens": doc.token_estimate},
                token_estimate=sum(ch.token_estimate for ch in relevant),
                size_category="medium",
            )
            prepared.append(filtered)
        else:
            # large >12k: retrieval/chunking path — for Phase 2A treat as medium with max 3 chunks
            relevant = select_relevant_chunks(doc, question, max_chunks=3)
            if not relevant:
                # fallback: first chunk
                relevant = chunk_document(doc)[:3]
            filtered_tables = [ch.table_slice for ch in relevant if ch.table_slice]
            filtered_text = "\n\n".join(ch.text for ch in relevant if ch.text)
            chunk_sources_large = [ch.source for ch in relevant if ch.source]
            chunk_source_set_large = set(chunk_sources_large)
            retained_sections_large = [
                s
                for s in doc.sections
                if s.source in chunk_source_set_large
                or s.title in chunk_source_set_large
                or f"Section: {s.title}" in chunk_source_set_large
                or any(s.title in src or (s.source and s.source in src) for src in chunk_source_set_large)
            ]
            prepared.append(
                DocumentContent(
                    filename=doc.filename,
                    file_type=doc.file_type,
                    text=filtered_text or doc.text[:8000],
                    tables=filtered_tables or doc.tables[:1],
                    sheets=doc.sheets,
                    pages=doc.pages,
                    sections=retained_sections_large,
                    metadata={**doc.metadata, "chunked": True, "truncated_large": True, "original_tokens": doc.token_estimate, "chunk_sources": chunk_sources_large},
                    token_estimate=sum(ch.token_estimate for ch in relevant) if relevant else doc.token_estimate,
                    size_category="large",
                )
            )
    return prepared
