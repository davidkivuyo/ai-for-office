"""Database service layer per AGENTS §16 — owns connection, timeouts, binding, limits.

Thin wrapper around query helpers for repository-style calls.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.query import execute_read_query
from app.db.result import DatabaseResult


async def run_read_only_query(
    session: AsyncSession,
    sql: str,
    params: dict[str, Any] | None = None,
    **kwargs: Any,
) -> DatabaseResult:
    """Service entry point — delegates to query helpers with limits."""
    return await execute_read_query(session, sql, params, **kwargs)


# Example repository-backed helpers could be added here per actual schema in Phase 2C.
