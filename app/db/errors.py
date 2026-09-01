"""Database error handling per AGENTS §27.

Never expose raw DB errors to the user. Convert internals to safe
application messages while logging technical details with request_id.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    """Application-level database error with safe user message."""

    def __init__(
        self,
        message: str = "The database request could not be completed right now.",
        *,
        request_id: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id or str(uuid.uuid4())[:8]
        self.cause = cause
        self.user_message = message

    def log(self, **extra: Any) -> None:
        logger.error(
            "database_error request_id=%s error=%s extra=%s",
            self.request_id,
            self.cause or self.args[0],
            extra,
            exc_info=self.cause,
        )


def handle_db_exception(exc: Exception, *, request_id: str | None = None, context: str = "") -> DatabaseError:
    """Convert raw DB exception into safe DatabaseError and log internally.

    Rules per §27:
    - Do not leak SQL, driver internals, or stack to user.
    - Log technical error with request_id.
    - Return generic safe message for API response.
    """
    rid = request_id or str(uuid.uuid4())[:8]
    # Map known driver timeout / connection patterns to specific safe messages if desired,
    # but keep user-facing text generic to avoid enumeration.
    safe_message = "The database request could not be completed right now."
    # Log full detail internally
    logger.error(
        "db_operation_failed request_id=%s context=%s error_type=%s error=%s",
        rid,
        context,
        type(exc).__name__,
        str(exc),
        exc_info=exc,
    )
    return DatabaseError(safe_message, request_id=rid, cause=exc)


# Specific subclasses for internal routing (still safe externally)
class DatabaseTimeoutError(DatabaseError):
    pass


class DatabasePermissionError(DatabaseError):
    def __init__(self, message: str = "You do not have permission to perform this database operation.", *, request_id: str | None = None, cause: Exception | None = None) -> None:
        super().__init__(message, request_id=request_id, cause=cause)


class DatabaseValidationError(DatabaseError):
    def __init__(self, message: str = "Invalid database request.", *, request_id: str | None = None, cause: Exception | None = None) -> None:
        super().__init__(message, request_id=request_id, cause=cause)
