"""Audit logging per AGENTS §25.

Every database tool call must record:
  request_id, user_id, conversation_id, tool_name, arguments,
  database_object, result_row_count, duration_ms, success/failure, timestamp

Do not automatically record full sensitive result data.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("app.db.audit")


@dataclass
class AuditRecord:
    request_id: str
    user_id: str | None
    conversation_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    database_object: str | None
    result_row_count: int | None
    duration_ms: int | None
    success: bool
    error: str | None
    timestamp: str  # ISO8601


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_arguments(args: dict[str, Any]) -> dict[str, str]:
    """Redact argument values to names/types only (§25, Security).

    Prevents sensitive values (emails, IDs) from reaching application logs.
    Keeps keys and value types for debugging without exposing content.
    """
    redacted: dict[str, str] = {}
    for k, v in args.items():
        if v is None:
            redacted[k] = "None"
        elif isinstance(v, str):
            # Keep length hint but not content
            redacted[k] = f"<redacted:str:len={len(v)}>"
        elif isinstance(v, (int, float, bool)):
            redacted[k] = f"<redacted:{type(v).__name__}>"
        elif isinstance(v, (list, tuple, set)):
            redacted[k] = f"<redacted:{type(v).__name__}:len={len(v)}>"
        elif isinstance(v, dict):
            redacted[k] = f"<redacted:dict:keys={','.join(sorted(v.keys()))}>"
        else:
            redacted[k] = f"<redacted:{type(v).__name__}>"
    return redacted


def _redacted_audit_dict(record: AuditRecord) -> dict[str, Any]:
    """Return audit dict with redacted arguments for safe logging."""
    d = asdict(record)
    # Redact sensitive arguments before writing to log
    if isinstance(d.get("arguments"), dict):
        d["arguments"] = _redact_arguments(d["arguments"])
    return d


def log_db_tool_call(
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    database_object: str | None = None,
    result_row_count: int | None = None,
    duration_ms: int | None = None,
    success: bool = True,
    error: str | None = None,
) -> AuditRecord:
    """Emit structured audit log via logger; returns AuditRecord for testing."""
    rid = request_id or str(uuid.uuid4())[:8]
    record = AuditRecord(
        request_id=rid,
        user_id=user_id,
        conversation_id=conversation_id,
        tool_name=tool_name,
        arguments=arguments or {},
        database_object=database_object,
        result_row_count=result_row_count,
        duration_ms=duration_ms,
        success=success,
        error=error,
        timestamp=_now_iso(),
    )
    # Structured JSON-like log; never log full sensitive result data or argument values (§25)
    redacted = _redacted_audit_dict(record)
    logger.info(
        "db_audit request_id=%s user_id=%s conversation_id=%s tool=%s db_object=%s rows=%s duration_ms=%s success=%s",
        record.request_id,
        record.user_id,
        record.conversation_id,
        record.tool_name,
        record.database_object,
        record.result_row_count,
        record.duration_ms,
        record.success,
        extra={"audit": redacted},
    )
    if not success and error:
        logger.warning("db_audit_error request_id=%s tool=%s error=%s", rid, tool_name, error)
    return record


class AuditTimer:
    """Context helper to measure duration_ms for audit."""

    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.duration_ms: int | None = None

    def stop(self) -> int:
        self.duration_ms = int((time.perf_counter() - self.start) * 1000)
        return self.duration_ms

    def __enter__(self) -> "AuditTimer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
