from __future__ import annotations

from app.files.models import DocumentContent


SYSTEM_PROMPT = (
    "You are an office document assistant.\n"
    "Use only the supplied document content.\n"
    "If the answer is not present, say so."
)


def format_document_for_llm(
    doc: DocumentContent, *, max_cell_length: int = 4000, max_total_chars: int = 16000
) -> str:
    """Build normalized DOCUMENT block per AGENTS §10-11.

    For tabular content, includes metadata and TABLE rows. For text, includes text with citations.
    Truncates very long cells to keep context efficient (§29).
    Enforces one overall character budget across rows/sections/text (§9).
    """
    lines: list[str] = []
    lines.append(f"File: {doc.filename}")
    lines.append(f"Type: {doc.file_type}")
    if doc.sheets:
        lines.append(f"Sheets: {', '.join(doc.sheets)}")
    if doc.pages is not None:
        lines.append(f"Pages: {doc.pages}")
    if doc.sections:
        lines.append(f"Sections: {', '.join(s.title for s in doc.sections)}")
    lines.append(f"Estimated tokens: {doc.token_estimate} ({doc.size_category})")
    lines.append("")

    # Overall budget across table rows, section content, and document text
    # Metadata lines above are always included; budget applies to content below
    remaining = max_total_chars
    truncated = False

    def _remaining_chars() -> int:
        # approximate remaining via current lines length + newlines
        used = sum(len(l) + 1 for l in lines)
        return max(0, max_total_chars - used)

    def _append_with_budget(text: str, *, is_metadata: bool = False) -> bool:
        nonlocal remaining, truncated
        # Metadata lines (DOCUMENT METADATA, TABLE header) are small; still count but allow
        # For content rows/sections/text, enforce budget
        if is_metadata:
            lines.append(text)
            return True
        rem = _remaining_chars()
        if rem <= 0:
            truncated = True
            return False
        if len(text) > rem:
            # truncate this piece to remaining and mark
            if rem > 20:
                lines.append(text[: rem - 20] + "…[truncated]")
            else:
                lines.append("…[truncated - budget exhausted]")
            truncated = True
            return False
        lines.append(text)
        return True

    # Tables — structured per AGENTS §10
    if doc.tables:
        for table in doc.tables:
            if not _append_with_budget("DOCUMENT METADATA", is_metadata=True):
                break
            if not _append_with_budget(f"file: {doc.filename}", is_metadata=True):
                break
            if table.name:
                if not _append_with_budget(f"sheet: {table.name}", is_metadata=True):
                    break
            if table.headers:
                if not _append_with_budget(f"columns: {', '.join(str(h) for h in table.headers)}", is_metadata=True):
                    break
            if table.source:
                if not _append_with_budget(f"source: {table.source}", is_metadata=True):
                    break
            if not _append_with_budget("", is_metadata=True):
                break
            if not _append_with_budget("TABLE", is_metadata=True):
                break
            # header
            if table.headers:
                # truncate long headers (per-cell)
                hdr = [str(h)[:max_cell_length] if len(str(h)) > max_cell_length else str(h) for h in table.headers]
                if not _append_with_budget(" | ".join(hdr)):
                    break
                if not _append_with_budget(" | ".join("---" for _ in hdr)):
                    break
            # rows — enforce global budget; chunking handles large
            for row in table.rows:
                cells = []
                for c in row:
                    s = "" if c is None else str(c)
                    if len(s) > max_cell_length:
                        s = s[:max_cell_length] + "…"
                    cells.append(s)
                row_line = " | ".join(cells)
                if not _append_with_budget(row_line):
                    # stop adding rows for this table and overall
                    truncated = True
                    break
            if truncated and _remaining_chars() <= 0:
                break
            if not _append_with_budget(""):
                break
            if truncated:
                break

    # Sections for DOCX
    if not truncated and doc.sections:
        for sec in doc.sections:
            if not _append_with_budget(f"SECTION: {sec.title}", is_metadata=True):
                truncated = True
                break
            if sec.source:
                if not _append_with_budget(f"source: {sec.source}", is_metadata=True):
                    truncated = True
                    break
            # truncate section content if absurdly long (per-section) within global cap
            content = sec.content
            if len(content) > 8000:
                content = content[:8000] + "…"
            if not _append_with_budget(content):
                break
            if not _append_with_budget(""):
                break
            if truncated:
                break

    # Remaining free text (if not already fully represented by tables/sections)
    if not truncated and doc.text:
        # Avoid duplicating if text is empty or already in tables/sections? For mixed docs, include.
        # We include text block; extractor already ensures text is normalized.
        # If doc has both tables and text, text may be redundant for spreadsheets — skip if spreadsheet-only?
        is_sheet_only = doc.file_type in ("xlsx", "csv") and doc.tables
        if not is_sheet_only:
            if not _append_with_budget("DOCUMENT TEXT", is_metadata=True):
                truncated = True
            elif doc.pages:
                if not _append_with_budget(f"source: {doc.filename} (pages: {doc.pages})", is_metadata=True):
                    truncated = True
            if not truncated:
                text = doc.text
                # Per-piece 12000 limit remains within global cap
                if len(text) > 12000:
                    text = text[:12000] + "\n…[truncated]"
                _append_with_budget(text)

    # Clear truncation marker if we stopped due to budget (already appended)
    if truncated and not any("…[truncated" in l for l in lines[-3:]):
        # ensure marker present
        rem = _remaining_chars()
        if rem > 0:
            lines.append("…[truncated - document truncated to budget]")

    return "\n".join(lines).strip()


def build_file_messages(doc: DocumentContent, question: str) -> list[dict[str, str]]:
    """Build messages list for file Q&A per AGENTS §10.

    Returns list suitable for provider.chat(messages=...).
    """
    document_block = format_document_for_llm(doc)
    user_block = f"USER REQUEST\n{question.strip()}"
    # Combine document + user request into a single user message with system prefix
    # System role is separate
    content = f"DOCUMENT\n{document_block}\n\n{user_block}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def build_chat_with_files_messages(
    history: list[dict[str, str]],
    new_question: str,
    docs: list[DocumentContent],
) -> list[dict[str, str]]:
    """Build full chat history including file contexts.

    Per AGENTS §29: provide metadata separately, use structured tables, extract only relevant sheets.
    For small files: include full document block. For medium: include only top relevant chunks.
    This helper assumes docs are already filtered to relevant chunks if needed.
    """
    # Build file context block
    if not docs:
        # no files — just normal chat
        msgs = []
        for m in history:
            if m.get("role") in ("user", "assistant", "system"):
                msgs.append({"role": m["role"], "content": m["content"]})
        msgs.append({"role": "user", "content": new_question})
        return msgs

    # For each doc, format
    doc_blocks = []
    for doc in docs:
        doc_blocks.append(format_document_for_llm(doc))

    combined_doc = "\n\n---\n\n".join(doc_blocks)
    # System remains first
    system_content = SYSTEM_PROMPT + "\n\nYou will be given file context. Cite source location where practical (e.g., Sheet: July Rows: 14-22, Page: 7)."
    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    # Add history (excluding system)
    for m in history:
        if m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})
    # Final user message with document + question
    final_user = f"DOCUMENT\n{combined_doc}\n\nUSER REQUEST\n{new_question.strip()}"
    messages.append({"role": "user", "content": final_user})
    return messages
