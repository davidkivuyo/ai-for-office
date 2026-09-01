"""Connection health check per AGENTS §15/§16 and Phase 2B requirement #4.

Provides:
- quick liveness probe (SELECT 1)
- pool validation via pool_pre_ping (already enabled at engine level)
- timeout-bounded check
- safe error handling (never leak raw DB errors)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.db.errors import handle_db_exception

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    status: str  # "ok" | "degraded" | "error"
    latency_ms: int | None = None
    error: str | None = None
    details: dict | None = None


async def check_database_health(
    engine: AsyncEngine,
    *,
    timeout_seconds: int | None = None,
    request_id: str | None = None,
) -> HealthStatus:
    """Perform a bounded health probe against the database engine.

    Uses SELECT 1 as liveness signal (§22 connection health check).
    Enforces timeout to avoid hanging health endpoints.
    """
    settings = get_settings()
    timeout = float(timeout_seconds if timeout_seconds is not None else settings.effective_db_query_timeout)
    start = time.perf_counter()

    try:
        async def _probe():  # type: ignore[no-untyped-def]
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                # Optionally check DB is not in read-only violation mode
                return True

        await asyncio.wait_for(_probe(), timeout=timeout)
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.debug("db_health_ok latency_ms=%s", latency_ms)
        return HealthStatus(status="ok", latency_ms=latency_ms)

    except asyncio.TimeoutError as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        err = handle_db_exception(TimeoutError(f"Health check timed out after {timeout}s"), request_id=request_id, context="health_check")
        logger.warning("db_health_timeout latency_ms=%s", latency_ms)
        return HealthStatus(status="error", latency_ms=latency_ms, error=err.user_message, details={"timeout": True})

    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        err = handle_db_exception(exc, request_id=request_id, context="health_check")
        # Map to degraded/error per existing /api/health behavior
        logger.warning("db_health_failed latency_ms=%s error=%s", latency_ms, type(exc).__name__)
        return HealthStatus(status="error", latency_ms=latency_ms, error=err.user_message, details={"exception": type(exc).__name__})


async def is_database_ready(engine: AsyncEngine) -> bool:
    """Convenience: True if health status is ok."""
    st = await check_database_health(engine)
    return st.status == "ok"
