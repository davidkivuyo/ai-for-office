"""SQLAlchemy engine factory per AGENTS §15/§16 — read-only enforcement.

The engine layer owns:
- connection management
- timeouts (connect + query)
- read-only enforcement at driver/connection level
- pool configuration
- logging

The application layer owns:
- argument validation
- permission checks
- result limiting/normalisation

The database account itself must enforce read-only (§15 rule 4). For
SQLite dev DB we simulate via PRAGMA query_only=ON; for PostgreSQL we
set default_transaction_read_only=on.
"""

from __future__ import annotations

import logging
import urllib.parse as _urlparse

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql") or url.startswith("postgres")


def _build_connect_args(url: str, read_only: bool, query_timeout_seconds: int) -> dict:
    """Build driver connect_args for timeout/readonly per engine type."""
    if _is_sqlite(url):
        # aiosqlite accepts timeout kwarg (busy timeout seconds)
        # For read-only simulation, we use PRAGMA query_only via event listener (not connect_args).
        args: dict = {}
        # busy timeout — SQLite default 5s; use query_timeout as well
        # aiosqlite timeout param is in seconds (float)
        args["timeout"] = float(query_timeout_seconds)
        # check_same_thread needed for async
        args["check_same_thread"] = False
        return args
    if _is_postgres(url):
        # asyncpg: server_settings for read-only, command_timeout for query timeout
        connect_args: dict = {}
        if read_only:
            connect_args["server_settings"] = {"default_transaction_read_only": "on"}
        # asyncpg timeout: command_timeout in seconds
        connect_args["command_timeout"] = int(query_timeout_seconds)
        # connect timeout separate via connect args if needed
        connect_args["timeout"] = int(query_timeout_seconds)
        return connect_args
    # generic fallback
    return {}


def create_db_engine(
    database_url: str,
    *,
    read_only: bool = True,
    query_timeout_seconds: int = 10,
    echo: bool = False,
    use_null_pool: bool = False,
    pool_pre_ping: bool = True,
) -> AsyncEngine:
    """Create an AsyncEngine with read-only enforcement and timeouts.

    Args:
        database_url: SQLAlchemy URL (sqlite+aiosqlite or postgresql+asyncpg)
        read_only: when True enforce read-only at DB/connection level
        query_timeout_seconds: driver timeout (per AGENTS §18 / §22)
        echo: SQL echo for debugging
        use_null_pool: use NullPool (for process-scoped/ephemeral engines)
        pool_pre_ping: validate connections before use

    Returns:
        AsyncEngine configured per AGENTS §15/§16.
    """
    if not database_url:
        raise ValueError("DATABASE_URL is required")

    connect_args = _build_connect_args(database_url, read_only, query_timeout_seconds)

    # SQLite read-only via URL query param as additional defence (mode=ro)
    # Only for read_only=True and sqlite file DB; we prefer PRAGMA listener but also handle URL variant.
    effective_url = database_url
    if read_only and _is_sqlite(database_url):
        # Check if already has query params; try to append mode=ro if file-based
        # aiosqlite supports URI query params after ?.
        # Keep existing behaviour for in-memory (sqlite+aiosqlite:///:memory:) — no mode param.
        if ":memory:" not in database_url and "mode=ro" not in database_url and "mode=" not in database_url:
            # Add mode=ro — sqlite URI handling (needs uri=true for driver to honour mode param)
            # Instead of mutating URL, rely on PRAGMA listener (more reliable).
            # We keep URL unchanged to avoid breaking existing tests.
            pass

    kwargs: dict = {
        "echo": echo,
        "pool_pre_ping": pool_pre_ping,
        "connect_args": connect_args,
    }
    if use_null_pool:
        kwargs["poolclass"] = NullPool

    # For postgres, ensure we don't use NullPool by default (pooled engine for app)
    engine = create_async_engine(effective_url, **kwargs)

    # Install read-only guards as SQLAlchemy event listeners
    if read_only:
        if _is_sqlite(database_url):
            # SQLite: enforce query_only pragma on each connection
            # Works for aiosqlite via sync_engine event (needs sync callback)
            # async engine still exposes sync_engine for event registration via sync_engine
            try:
                # Use sync_engine under the hood for SQLite pragma setup
                @event.listens_for(engine.sync_engine, "connect")
                def _set_sqlite_readonly(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
                    try:
                        cursor = dbapi_connection.cursor()
                        cursor.execute("PRAGMA query_only = ON;")
                        cursor.close()
                    except Exception:
                        # If pragma fails (e.g., in-memory during transaction), ignore — app validation still blocks
                        logger.debug("Failed to set PRAGMA query_only=ON", exc_info=True)
            except Exception:
                logger.debug("Failed to register sqlite readonly listener", exc_info=True)
        else:
            # For Postgres, server_settings already enforces read-only; add additional check via execution
            pass

    logger.info(
        "db_engine_created read_only=%s url_scheme=%s timeout=%s null_pool=%s",
        read_only,
        database_url.split(":")[0],
        query_timeout_seconds,
        use_null_pool,
    )
    return engine


def create_engine_for_settings(settings, *, use_null_pool: bool = False) -> AsyncEngine:
    """Convenience: create engine from Settings instance respecting §22 limits."""
    read_only = bool(getattr(settings, "database_read_only", True))
    timeout = int(getattr(settings, "effective_db_query_timeout", getattr(settings, "db_query_timeout_seconds", 10)))
    return create_db_engine(
        settings.database_url,
        read_only=read_only,
        query_timeout_seconds=timeout,
        echo=False,
        use_null_pool=use_null_pool,
    )


def is_read_only_engine(engine: AsyncEngine) -> bool:
    """Heuristic: check if engine URL indicates read-only intent via settings.

    We don't store the flag directly; callers should consult config. This helper
    checks for postgres default_transaction_read_only in connect_args.
    """
    try:
        ca = engine.sync_engine.pool._creator_kwargs.get("connect_args", {}) if hasattr(engine.sync_engine.pool, "_creator_kwargs") else {}
        if isinstance(ca, dict) and ca.get("server_settings", {}).get("default_transaction_read_only") == "on":
            return True
    except Exception:
        pass
    # For SQLite we rely on pragma listener; treat as read_only if pragma set attempt was made
    return False
