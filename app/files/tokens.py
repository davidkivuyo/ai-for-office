from __future__ import annotations

# Token estimation per AGENTS §8 — use extracted text length, not file bytes.
# Keep heuristic consistent with provider's estimate (~4 chars/token).
# Must be lightweight for 12GB laptops; no tiktoken tokenizer by default.


def estimate_tokens(text: str) -> int:
    """Estimate token count from extracted text.

    Uses 4 chars ≈ 1 token heuristic. Suitable for Phase 2 lightweight policy.
    """
    if not text:
        return 0
    # strip then count; empty string => 0
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 3) // 4)


def estimate_tokens_for_tables(tables) -> int:
    """Sum tokens for table contents (headers + cell values)."""
    total = 0
    for t in tables:
        # headers
        for h in getattr(t, "headers", []) or []:
            total += estimate_tokens(str(h))
        # rows
        for row in getattr(t, "rows", []) or []:
            for cell in row:
                total += estimate_tokens(str(cell)) if cell is not None else 0
        # name/source
        if getattr(t, "name", None):
            total += estimate_tokens(str(t.name))
    return total


def categorize_size(token_count: int) -> str:
    """Categorize per AGENTS §8 thresholds.

    <4,000 -> small (direct context)
    4,000-12,000 -> medium (structured chunking)
    >12,000 -> large (retrieval/chunking path)
    """
    if token_count < 4000:
        return "small"
    if token_count <= 12000:
        return "medium"
    return "large"
