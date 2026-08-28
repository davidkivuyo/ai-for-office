from __future__ import annotations

from dataclasses import dataclass

from app.files.models import DocumentContent, DocumentTable
from app.files.tokens import estimate_tokens


@dataclass
class DocumentChunk:
    """Chunk for medium/large files — preserves source citation."""

    text: str
    table_slice: DocumentTable | None = None
    source: str | None = None
    chunk_index: int = 0
    token_estimate: int = 0


def chunk_text(text: str, *, chunk_tokens: int = 1000, overlap_tokens: int = 100) -> list[str]:
    """Naive char-based chunking ~ chunk_tokens each with overlap.

    Keeps CPU minimal — no embedding, just slicing.
    """
    if not text or not text.strip():
        return []
    # approx chars per chunk
    chars_per_chunk = chunk_tokens * 4
    overlap_chars = overlap_tokens * 4
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chars_per_chunk, n)
        # try to break on paragraph boundary near end
        if end < n:
            # look back for \n\n within last 20% of chunk
            window_start = end - chars_per_chunk // 5
            snippet = text[window_start:end]
            last_break = snippet.rfind("\n\n")
            if last_break != -1:
                end = window_start + last_break + 2
            else:
                last_nl = text.rfind("\n", window_start, end)
                if last_nl != -1 and last_nl > start + chars_per_chunk // 2:
                    end = last_nl + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = end - overlap_chars
        if start < 0:
            start = 0
    return chunks


def chunk_document(doc: DocumentContent, *, chunk_tokens: int = 1000, overlap_tokens: int = 100) -> list[DocumentChunk]:
    """Chunk DocumentContent for medium path per AGENTS §30.

    - For text-heavy docs (txt/md/pdf/docx): chunk text with source preservation.
    - For spreadsheet: chunk by sheet/rows, preserving headers.
    - Returns list of DocumentChunk with citation source.
    """
    chunks: list[DocumentChunk] = []
    idx = 0

    # If doc has tables (spreadsheet/csv), chunk tables separately
    if doc.tables:
        for table in doc.tables:
            # For small tables, keep whole table as one chunk
            header_text = " | ".join(table.headers) if table.headers else ""
            # Estimate rows per chunk so each chunk ~ chunk_tokens
            # overhead for headers
            header_tokens = estimate_tokens(header_text) if header_text else 0
            remaining_tokens = max(100, chunk_tokens - header_tokens)
            # approximate rows per chunk
            # avg row tokens
            if not table.rows:
                # header-only table
                chunk_text_val = header_text
                chunks.append(
                    DocumentChunk(
                        text=chunk_text_val,
                        table_slice=table,
                        source=table.source if table.source else (f"Sheet: {table.name}" if table.name else doc.filename),
                        chunk_index=idx,
                        token_estimate=estimate_tokens(chunk_text_val),
                    )
                )
                idx += 1
                continue

            # slice rows
            start = 0
            while start < len(table.rows):
                # collect rows until token budget
                acc_tokens = 0
                end = start
                slice_rows: list[list] = []
                while end < len(table.rows) and acc_tokens < remaining_tokens:
                    row = table.rows[end]
                    row_text = " | ".join(str(c) if c is not None else "" for c in row)
                    rt = estimate_tokens(row_text)
                    if acc_tokens + rt > remaining_tokens and slice_rows:
                        break
                    slice_rows.append(row)
                    acc_tokens += rt
                    end += 1
                # build chunk text
                rows_text = "\n".join(" | ".join(str(c) if c is not None else "" for c in r) for r in slice_rows)
                chunk_text_val = (header_text + "\n" + rows_text).strip() if header_text else rows_text
                source = table.source
                if not source and table.name:
                    source = f"Sheet: {table.name} Rows: {start+1}-{end}"
                chunks.append(
                    DocumentChunk(
                        text=chunk_text_val,
                        table_slice=DocumentTable(
                            name=table.name,
                            headers=table.headers,
                            rows=slice_rows,
                            source=source,
                            metadata=table.metadata,
                        ),
                        source=source,
                        chunk_index=idx,
                        token_estimate=estimate_tokens(chunk_text_val),
                    )
                )
                idx += 1
                if end >= len(table.rows):
                    break
                start = end
        # Also chunk remaining free text if any
        if doc.text and doc.text.strip():
            # text may already be represented in tables; but for mixed docs (pdf with tables + text), chunk it
            # Only chunk text that isn't duplicated in tables? For now chunk it as well but mark source
            text_chunks = chunk_text(doc.text, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
            for tc in text_chunks:
                # avoid duplicating table-only docs where text is derived from tables — check if text chunk is subset
                chunks.append(
                    DocumentChunk(
                        text=tc,
                        table_slice=None,
                        source=doc.filename,
                        chunk_index=idx,
                        token_estimate=estimate_tokens(tc),
                    )
                )
                idx += 1
        return chunks

    # Text-heavy path: chunk text with page/section sources if available
    if doc.sections:
        for sec in doc.sections:
            text_chunks = chunk_text(sec.content, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
            if not text_chunks:
                continue
            for tc in text_chunks:
                chunks.append(
                    DocumentChunk(
                        text=tc,
                        table_slice=None,
                        source=sec.source or f"Section: {sec.title}",
                        chunk_index=idx,
                        token_estimate=estimate_tokens(tc),
                    )
                )
                idx += 1
        return chunks

    if doc.pages is not None and doc.text:
        # PDF with page metadata — we already have per-page chunks via extractor? Fall back to generic
        text_chunks = chunk_text(doc.text, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
        for tc in text_chunks:
            chunks.append(
                DocumentChunk(
                    text=tc,
                    table_slice=None,
                    source=doc.filename,
                    chunk_index=idx,
                    token_estimate=estimate_tokens(tc),
                )
            )
            idx += 1
        return chunks

    # generic text
    text_chunks = chunk_text(doc.text or "", chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
    for tc in text_chunks:
        chunks.append(
            DocumentChunk(
                text=tc,
                table_slice=None,
                source=doc.filename,
                chunk_index=idx,
                token_estimate=estimate_tokens(tc),
            )
        )
        idx += 1
    return chunks


def select_relevant_chunks(doc: DocumentContent, query: str, *, max_chunks: int = 3) -> list[DocumentChunk]:
    """Lightweight relevance: keyword overlap, no embeddings.

    For Phase 2A medium path, retrieve only relevant sheets/rows/pages.
    Uses simple term overlap to keep CPU low.
    """
    if not query or not query.strip():
        # return first chunks
        all_chunks = chunk_document(doc)
        return all_chunks[:max_chunks]

    all_chunks = chunk_document(doc)
    if len(all_chunks) <= max_chunks:
        return all_chunks

    query_terms = set(query.lower().split())
    scored: list[tuple[int, DocumentChunk]] = []
    for ch in all_chunks:
        text_lower = ch.text.lower()
        score = sum(1 for term in query_terms if term in text_lower)
        # bonus for table chunks where header matches
        if ch.table_slice and ch.table_slice.headers:
            header_text = " ".join(h.lower() for h in ch.table_slice.headers)
            score += sum(2 for term in query_terms if term in header_text)
        scored.append((score, ch))
    # sort by score desc, then original order
    scored.sort(key=lambda x: (-x[0], x[1].chunk_index))
    # if all zero scores, fall back to first N
    if all(s == 0 for s, _ in scored):
        return all_chunks[:max_chunks]
    return [ch for _, ch in scored[:max_chunks]]
