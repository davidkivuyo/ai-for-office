"""Parameterized query helpers with validation, timeouts and result limits per AGENTS §16/§18/§22.

Provides:
- SELECT-only single-statement enforcement
- allowlisted schemas/tables (optional)
- parameterized values (no string interpolation)
- automatic LIMIT + timeout
- cell length truncation
- result normalization
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.audit import AuditTimer, log_db_tool_call
from app.db.errors import DatabaseError, DatabaseValidationError, handle_db_exception
from app.db.result import DatabaseResult, normalize_rows

logger = logging.getLogger(__name__)

# Security: blocklist patterns per AGENTS §18 — no DDL/DML, no multiple statements, no comment bypass
_BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|REPLACE|MERGE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)
# Detect multiple statements (semicolon not inside quotes) — conservative: any semicolon besides trailing
_MULTIPLE_STMT_RE = re.compile(r";\s*\S")
_COMMENT_RE = re.compile(r"(--|/\*|#)")


def validate_select_only(sql: str) -> None:
    """Enforce SELECT-only, single-statement, no comments to bypass parsing (§18)."""
    if not sql or not sql.strip():
        raise DatabaseValidationError("Empty query")
    s = sql.strip()
    # Strip trailing semicolon for analysis
    s_nosemi = s.rstrip().rstrip(";").strip()
    # Must start with SELECT or WITH (CTE)
    if not re.match(r"^\s*(SELECT|WITH)\b", s_nosemi, re.IGNORECASE):
        raise DatabaseValidationError("Only SELECT statements are allowed")
    # Block DDL/DML keywords anywhere (even inside subqueries — we only want reads)
    if _BLOCKED_KEYWORDS.search(s_nosemi):
        raise DatabaseValidationError("Only SELECT operations are allowed (read-only)")
    # Multiple statements
    # Count semicolons not at end — if any content after semicolon, reject
    if _MULTIPLE_STMT_RE.search(s):
        raise DatabaseValidationError("Multiple statements not allowed")
    # No SQL comments used to bypass parsing (§18)
    if _COMMENT_RE.search(s_nosemi):
        # Allow if comment appears after validated content? Strict: reject any comment markers
        # Check that comment chars are not just inside a string literal: simplistic reject
        raise DatabaseValidationError("SQL comments are not allowed")

    # Additional: ensure single statement — no embedded semicolon before end
    if ";" in s_nosemi:
        raise DatabaseValidationError("Multiple statements not allowed")


def _ensure_limit(sql: str, max_rows: int) -> str:
    """Enforce service-owned row limit (§18/§22).

    - If no LIMIT, append LIMIT max_rows.
    - If outer LIMIT exists, cap it to min(existing, max_rows).
    - If LIMIT exists only inside subqueries, wrap query to enforce outer cap.

    Prevents bypass like `LIMIT 100000000` exhausting worker memory via fetchall().
    """
    max_rows = int(max_rows)
    if max_rows <= 0:
        raise DatabaseValidationError("max_rows must be positive")
    sql_stripped = sql.strip().rstrip(";").strip()
    # Outer LIMIT at end (with optional OFFSET)
    m = re.search(r"\bLIMIT\s+(\d+)(?:\s+OFFSET\s+\d+)?\s*$", sql_stripped, re.IGNORECASE)
    if m:
        try:
            existing = int(m.group(1))
            if existing > max_rows:
                # Preserve OFFSET if present
                offset_m = re.search(r"\bLIMIT\s+\d+\s+(OFFSET\s+\d+)\s*$", sql_stripped, re.IGNORECASE)
                if offset_m:
                    offset_part = offset_m.group(1)
                    return re.sub(
                        r"\bLIMIT\s+\d+(?:\s+OFFSET\s+\d+)?\s*$",
                        f"LIMIT {max_rows} {offset_part}",
                        sql_stripped,
                        flags=re.IGNORECASE,
                    )
                return re.sub(
                    r"\bLIMIT\s+\d+(?:\s+OFFSET\s+\d+)?\s*$",
                    f"LIMIT {max_rows}",
                    sql_stripped,
                    flags=re.IGNORECASE,
                )
            # Existing limit is tighter → keep as-is (still within cap)
            return sql_stripped
        except ValueError:
            pass
    # LIMIT exists somewhere but not as outer cap (e.g., inside subquery) → enforce via wrapper
    if re.search(r"\bLIMIT\s+\d+", sql_stripped, re.IGNORECASE):
        return f"SELECT * FROM ({sql_stripped}) AS _svc_capped LIMIT {max_rows}"
    return f"{sql_stripped} LIMIT {max_rows}"


def _sanitize_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Ensure params are dict and only contain serializable values; no injection vectors."""
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise DatabaseValidationError("Parameters must be a dict")
    # No need to deeply validate values — SQLAlchemy will bind them safely
    return params


async def execute_read_query(
    session: AsyncSession,
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    max_rows: int | None = None,
    max_cell_length: int | None = None,
    query_timeout_seconds: int | None = None,
    user_id: str | None = None,
    request_id: str | None = None,
    tool_name: str = "service_query",
    database_object: str | None = None,
) -> DatabaseResult:
    """Execute a read-only parameterized query with validation, limits and audit.

    Args:
        session: AsyncSession to execute within
        sql: SELECT-only SQL with named binds e.g. :param
        params: bound parameters (parameterized values)
        max_rows: row limit (defaults to Settings.effective_db_max_rows)
        max_cell_length: cell truncation (Settings.db_max_cell_length)
        query_timeout_seconds: timeout (Settings.effective_db_query_timeout)
        user_id/request_id: audit fields
        tool_name: audit tool name
        database_object: audit DB object

    Returns:
        DatabaseResult normalized per §21

    Raises:
        DatabaseValidationError — blocked query
        DatabaseError — execution failure (safe message)
    """
    settings = get_settings()
    rid = request_id or str(uuid.uuid4())[:8]
    effective_max_rows = int(max_rows if max_rows is not None else settings.effective_db_max_rows)
    effective_cell = int(max_cell_length if max_cell_length is not None else settings.db_max_cell_length)
    effective_timeout = int(query_timeout_seconds if query_timeout_seconds is not None else settings.effective_db_query_timeout)
    params = _sanitize_params(params)

    # Validate outside DB (no injection, no DDL)
    validate_select_only(sql)
    sql_limited = _ensure_limit(sql, effective_max_rows)

    timer = AuditTimer()
    try:
        # Enforce query timeout via asyncio.wait_for (works for sqlite and async engines)
        # SQLAlchemy async execution may not honour DB-level timeout for sqlite, so we wrap
        start = time.perf_counter()

        async def _exec():  # type: ignore[no-untyped-def]
            result = await session.execute(text(sql_limited), params)
            # Fetch columns
            cols = list(result.keys()) if hasattr(result, "keys") else []
            # Use fetchmany to enforce memory cap — never load unbounded rows into worker memory
            # Fetch at most max_rows+1 to detect truncation without exhausting memory
            if hasattr(result, "fetchmany"):
                rows = result.fetchmany(effective_max_rows + 1)
            elif hasattr(result, "fetchall"):
                # Fallback: still cap via fetchmany logic if available via iteration
                all_rows = result.fetchall()
                rows = all_rows[: effective_max_rows + 1]
            else:
                rows = []
            return cols, rows

        try:
            cols, rows = await asyncio.wait_for(_exec(), timeout=float(effective_timeout))
        except asyncio.TimeoutError as e:
            raise handle_db_exception(
                TimeoutError(f"Query timed out after {effective_timeout}s"),
                request_id=rid,
                context=f"tool={tool_name} sql={sql_limited[:200]}",
            ) from e

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        # Note: rows may be Row objects; convert to tuples
        raw_rows = [tuple(r) if not isinstance(r, (list, tuple)) else tuple(r) for r in rows] if rows else []

        # Normalize with limits (§21/§22)
        db_result = normalize_rows(cols, raw_rows, max_rows=effective_max_rows, max_cell_length=effective_cell)
        db_result.execution_time_ms = elapsed_ms
        db_result.query = sql_limited

        log_db_tool_call(
            request_id=rid,
            user_id=user_id,
            tool_name=tool_name,
            arguments={"sql": sql_limited, **params},
            database_object=database_object,
            result_row_count=db_result.row_count,
            duration_ms=elapsed_ms,
            success=True,
        )
        return db_result

    except DatabaseError as db_err:
        # Audit failure for all DatabaseError paths (including timeout) — previously missed,
        # so timed-out requests had no structured audit event. Use sanitized error code.
        sanitized = "timeout" if isinstance(db_err.cause, TimeoutError) or "timed out" in str(db_err.cause).lower() else "database_error"
        log_db_tool_call(
            request_id=rid,
            user_id=user_id,
            tool_name=tool_name,
            arguments={"sql": sql_limited if "sql_limited" in locals() else sql, **params},
            database_object=database_object,
            result_row_count=None,
            duration_ms=timer.stop(),
            success=False,
            error=sanitized,
        )
        raise
    except Exception as exc:
        # Convert to safe error and audit failure
        db_err = handle_db_exception(exc, request_id=rid, context=f"tool={tool_name}")
        log_db_tool_call(
            request_id=rid,
            user_id=user_id,
            tool_name=tool_name,
            arguments={"sql": sql_limited if "sql_limited" in locals() else sql, **params},
            database_object=database_object,
            result_row_count=None,
            duration_ms=timer.stop(),
            success=False,
            error=str(exc)[:500],
        )
        raise db_err from exc


async def execute_parameterized_select(
    session: AsyncSession,
    base_sql: str,
    params: dict[str, Any],
    *,
    allowed_columns: set[str] | None = None,
    allowed_tables: set[str] | None = None,
    **kwargs: Any,
) -> DatabaseResult:
    """Helper that additionally checks allowlisted tables/columns if provided (§18)."""
    if allowed_tables is not None:
        # Simple check: ensure SQL mentions only allowed tables (table names)
        # This is a lightweight layer; full enforcement should be at repository/view level
        sql_lower = base_sql.lower()
        # Extract possible table references via regex FROM/JOIN
        found_tables = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_\.]*)", sql_lower))
        # Normalize schema-qualified names
        found_simple = {t.split(".")[-1] for t in found_tables}
        disallowed = found_simple - {t.lower() for t in allowed_tables}
        if disallowed:
            raise DatabaseValidationError(f"Access to tables {disallowed} not allowed")
    return await execute_read_query(session, base_sql, params, **kwargs)
