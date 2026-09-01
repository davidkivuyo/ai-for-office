"""Database result normalization per AGENTS §21/§22.

Normalized form sent to LLM:
    DatabaseResult(
        columns=[...],
        rows=[[...]],
        row_count=2,
        truncated=False,
    )

Large results summarized before passing to LLM; cell values truncated to DB_MAX_CELL_LENGTH.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DatabaseResult:
    """Predictable normalized result for LLM/tool consumption."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[object]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    # Optional metadata for observability
    execution_time_ms: int | None = None
    query: str | None = None

    def to_dict(self) -> dict:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "execution_time_ms": self.execution_time_ms,
        }

    def to_llm_payload(self) -> dict:
        """Compact payload for LLM context — omits internal timing/query."""
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
        }


def normalize_rows(
    columns: list[str],
    rows: list[tuple | list],
    *,
    max_rows: int = 200,
    max_cell_length: int = 4000,
) -> DatabaseResult:
    """Normalize raw DB rows into DatabaseResult with limits applied.

    - Enforces DB_MAX_ROWS truncation
    - Truncates overlong cell values to DB_MAX_CELL_LENGTH
    - Preserves column names for LLM header context
    """
    truncated = len(rows) > max_rows
    limited = rows[:max_rows]

    def _normalize_cell(value: object) -> object:
        # Preserve JSON primitives
        if value is None:
            return None
        # bool must be checked before int (bool is subclass of int)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            # JSON cannot handle NaN/Inf strictly; convert to string
            if value != value or value in (float("inf"), float("-inf")):  # NaN or Inf
                return str(value)
            return value
        # Decode bytes-like of any length to text (fixes short BLOB not being converted)
        if isinstance(value, (bytes, bytearray, memoryview)):
            try:
                if isinstance(value, memoryview):
                    # memoryview -> bytes
                    b = value.tobytes()
                elif isinstance(value, bytearray):
                    b = bytes(value)
                else:
                    b = value  # bytes
                s = b.decode("utf-8", errors="replace")
            except Exception:
                s = repr(value)
            if len(s) > max_cell_length:
                return s[:max_cell_length] + "…[truncated]"
            return s
        if isinstance(value, str):
            if len(value) > max_cell_length:
                return value[:max_cell_length] + "…[truncated]"
            return value
        # Datetime types -> ISO8601 string (JSON-safe)
        # Import locally to avoid overhead
        try:
            import datetime as _dt
            import decimal as _dec
            import uuid as _uuid

            if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
                s = value.isoformat()
                if len(s) > max_cell_length:
                    return s[:max_cell_length] + "…[truncated]"
                return s
            if isinstance(value, _dec.Decimal):
                # Preserve exact decimal as string (JSON number may lose precision)
                s = str(value)
                if len(s) > max_cell_length:
                    return s[:max_cell_length] + "…[truncated]"
                return s
            if isinstance(value, _uuid.UUID):
                s = str(value)
                if len(s) > max_cell_length:
                    return s[:max_cell_length] + "…[truncated]"
                return s
        except Exception:
            pass
        # Fallback for any other driver-native scalar (e.g., custom types) -> string
        try:
            s = str(value)
        except Exception:
            s = repr(value)
        if len(s) > max_cell_length:
            return s[:max_cell_length] + "…[truncated]"
        return s

    normalized_rows: list[list[object]] = []
    for row in limited:
        # row may be tuple from cursor or dict-like
        if isinstance(row, dict):
            # dict row: order by columns
            values = [row.get(col) for col in columns]
        else:
            values = list(row)
        # Normalize every cell to JSON-safe representation before LLM payload
        safe_vals: list[object] = [_normalize_cell(v) for v in values]
        normalized_rows.append(safe_vals)

    return DatabaseResult(
        columns=columns,
        rows=normalized_rows,
        row_count=len(normalized_rows),
        truncated=truncated,
    )
