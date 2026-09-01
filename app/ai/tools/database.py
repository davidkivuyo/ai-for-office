"""Database AI tool layer — Phase 2B foundation per AGENTS §16/§19.

The database service owns connection, timeouts, parameter binding, limits, transactions, logging.
The AI tool owns schema, permission checking, converting safe arguments into DB service requests.

Phase 2B: explicit typed tools only (No generic SQL). Implements AGENTS §17.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.audit import AuditTimer, log_db_tool_call
from app.db.errors import DatabasePermissionError, DatabaseValidationError
from app.db.models import User
from app.db.permissions import check_tool_permission
from app.db.result import DatabaseResult, normalize_rows

logger = logging.getLogger(__name__)


# Typed domain tools — explicit, validated, repository-backed per AGENTS §17
# Do not add generic SQL execution here (AGENTS §17: "Do Not Start With Arbitrary SQL")
ALLOWED_TOOL_NAMES = {
    "search_users",
    "get_user",
    "list_conversations",
    "get_conversation_messages",
    # Aliases for AGENTS example names (mapped to same safe implementations)
    "search_customers",
    "get_customer",
}


def _validate_search_users_args(args: dict[str, Any]) -> tuple[str, int]:
    term = args.get("search_term")
    if term is None:
        # also accept "search_term" vs "query" vs "term" for flexibility, but require one
        term = args.get("query") or args.get("term")
    if not isinstance(term, str) or not term.strip():
        raise DatabaseValidationError("search_term is required and must be a non-empty string")
    term = term.strip()
    if len(term) > 100:
        raise DatabaseValidationError("search_term too long (max 100 chars)")
    # Optional limit
    limit = args.get("limit", 10)
    try:
        limit = int(limit)
    except Exception:
        raise DatabaseValidationError("limit must be an integer")
    if limit <= 0 or limit > 50:
        raise DatabaseValidationError("limit must be between 1 and 50")
    return term, limit


def _validate_get_user_args(args: dict[str, Any]) -> dict[str, str]:
    username = args.get("username")
    user_id = args.get("user_id") or args.get("id") or args.get("customer_id")
    if username is not None:
        if not isinstance(username, str) or not username.strip():
            raise DatabaseValidationError("username must be a non-empty string")
        username = username.strip()
        if len(username) > 64:
            raise DatabaseValidationError("username too long")
        return {"username": username}
    if user_id is not None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise DatabaseValidationError("user_id must be a non-empty string")
        user_id = user_id.strip()
        if len(user_id) > 36:
            raise DatabaseValidationError("user_id too long")
        return {"user_id": user_id}
    raise DatabaseValidationError("get_user requires username or user_id")


def _validate_list_conversations_args(args: dict[str, Any]) -> tuple[str, int]:
    user_id = args.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise DatabaseValidationError("user_id is required")
    user_id = user_id.strip()
    limit = args.get("limit", 10)
    try:
        limit = int(limit)
    except Exception:
        raise DatabaseValidationError("limit must be integer")
    if limit <= 0 or limit > 50:
        raise DatabaseValidationError("limit must be between 1 and 50")
    return user_id, limit


def _validate_get_conversation_messages_args(args: dict[str, Any]) -> tuple[str, int]:
    conv_id = args.get("conversation_id") or args.get("id")
    if not isinstance(conv_id, str) or not conv_id.strip():
        raise DatabaseValidationError("conversation_id is required")
    conv_id = conv_id.strip()
    limit = args.get("limit", 20)
    try:
        limit = int(limit)
    except Exception:
        raise DatabaseValidationError("limit must be integer")
    if limit <= 0 or limit > 100:
        raise DatabaseValidationError("limit must be between 1 and 100")
    return conv_id, limit


def validate_tool_call(tool_name: str, arguments: dict[str, Any] | None, user_id: str | None = None) -> dict[str, Any]:
    """Validate tool name + arguments per §19 steps 1–2."""
    if not tool_name or not isinstance(tool_name, str):
        raise DatabaseValidationError("Invalid tool name")
    tool_name = tool_name.strip()
    if tool_name not in ALLOWED_TOOL_NAMES:
        # Generic SQL and unregistered tools are blocked — explicit allowlist only
        raise DatabaseValidationError(f"Tool {tool_name!r} is not allowed")
    args = arguments or {}
    if not isinstance(args, dict):
        raise DatabaseValidationError("Tool arguments must be an object")
    # Per-tool schema validation
    if tool_name in ("search_users", "search_customers"):
        term, limit = _validate_search_users_args(args)
        return {"search_term": term, "limit": limit}
    if tool_name in ("get_user", "get_customer"):
        validated = _validate_get_user_args(args)
        return validated
    if tool_name == "list_conversations":
        user_id_v, limit = _validate_list_conversations_args(args)
        return {"user_id": user_id_v, "limit": limit}
    if tool_name == "get_conversation_messages":
        conv_id, limit = _validate_get_conversation_messages_args(args)
        return {"conversation_id": conv_id, "limit": limit}
    return args


# --- Typed tool implementations (repository-backed, no raw SQL) ---

async def _execute_search_users(session: AsyncSession, args: dict[str, Any], max_rows: int) -> DatabaseResult:
    from app.db.models import User
    term = args["search_term"]
    limit = min(int(args.get("limit", 10)), max_rows)
    # Use parameterized LIKE via SQLAlchemy (no string interpolation)
    pattern = f"%{term}%"
    stmt = (
        select(User.id, User.username, User.display_name, User.is_active, User.created_at)
        .where(or_(User.username.like(pattern), User.display_name.like(pattern)))
        .order_by(User.username)
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.fetchall()
    cols = ["id", "username", "display_name", "is_active", "created_at"]
    # Convert to list of tuples
    raw = [tuple(r) for r in rows]
    # Enforce max_rows and cell truncation via normalize
    from app.config import get_settings
    settings = get_settings()
    return normalize_rows(cols, raw, max_rows=max_rows, max_cell_length=settings.db_max_cell_length)


async def _execute_get_user(session: AsyncSession, args: dict[str, Any], max_rows: int) -> DatabaseResult:
    from app.db.models import User
    from sqlalchemy import select
    stmt = None
    if "username" in args:
        stmt = select(User.id, User.username, User.display_name, User.is_active, User.created_at).where(User.username == args["username"]).limit(1)
    else:
        stmt = select(User.id, User.username, User.display_name, User.is_active, User.created_at).where(User.id == args["user_id"]).limit(1)
    result = await session.execute(stmt)
    rows = result.fetchall()
    cols = ["id", "username", "display_name", "is_active", "created_at"]
    raw = [tuple(r) for r in rows]
    from app.config import get_settings
    settings = get_settings()
    return normalize_rows(cols, raw, max_rows=max_rows, max_cell_length=settings.db_max_cell_length)


async def _execute_list_conversations(session: AsyncSession, args: dict[str, Any], max_rows: int, caller_id: str) -> DatabaseResult:
    from app.db.models import Conversation
    # Use verified caller_id for ownership (ignore spoofable args["user_id"])
    limit = min(int(args.get("limit", 10)), max_rows)
    stmt = (
        select(Conversation.id, Conversation.user_id, Conversation.title, Conversation.created_at, Conversation.updated_at)
        .where(Conversation.user_id == caller_id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.fetchall()
    cols = ["id", "user_id", "title", "created_at", "updated_at"]
    raw = [tuple(r) for r in rows]
    from app.config import get_settings
    settings = get_settings()
    return normalize_rows(cols, raw, max_rows=max_rows, max_cell_length=settings.db_max_cell_length)


async def _execute_get_conversation_messages(session: AsyncSession, args: dict[str, Any], max_rows: int, caller_id: str) -> DatabaseResult:
    from app.db.models import Conversation, Message
    conv_id = args["conversation_id"]
    limit = min(int(args.get("limit", 20)), max_rows)
    stmt = (
        select(Message.id, Message.conversation_id, Message.role, Message.content, Message.created_at)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.user_id == caller_id, Message.conversation_id == conv_id)
        .order_by(Message.created_at)
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.fetchall()
    cols = ["id", "conversation_id", "role", "content", "created_at"]
    raw = [tuple(r) for r in rows]
    from app.config import get_settings
    settings = get_settings()
    # Truncate long content via normalize
    return normalize_rows(cols, raw, max_rows=max_rows, max_cell_length=settings.db_max_cell_length)


async def execute_tool(
    session: AsyncSession,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    user: User | None = None,
    conversation_id: str | None = None,
    request_id: str | None = None,
    user_id: str | None = None,  # deprecated: derived from verified User, kept for compat
) -> DatabaseResult:
    rid = request_id or str(uuid.uuid4())[:8]
    # Require verified User before any database execution (§23, Security)
    if user is None or not isinstance(user, User):
        log_db_tool_call(
            request_id=rid,
            user_id=user_id,  # log attempted spoofed id if any, but don't trust it
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=arguments,
            success=False,
            error="permission_denied: missing verified user",
        )
        raise DatabasePermissionError("Authentication required for database tools", request_id=rid)
    if not user.is_active:
        log_db_tool_call(
            request_id=rid,
            user_id=user.id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=arguments,
            success=False,
            error="permission_denied: inactive user",
        )
        raise DatabasePermissionError("User inactive", request_id=rid)
    if not check_tool_permission(user, tool_name):
        log_db_tool_call(
            request_id=rid,
            user_id=user.id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=arguments,
            success=False,
            error="permission_denied",
        )
        raise DatabasePermissionError(f"Permission denied for tool {tool_name!r}", request_id=rid)
    # Use verified user.id for audit (ignore spoofable user_id param)
    audit_user_id = user.id
    timer = AuditTimer()
    try:
        # Block generic SQL execution — Phase 2B must use typed tools only (§17)
        if tool_name == "controlled_sql":
            raise DatabaseValidationError("Generic SQL execution is disabled in Phase 2B. Use typed tools (search_users, get_user, list_conversations).")
        args = validate_tool_call(tool_name, arguments, user_id=audit_user_id)
        from app.config import get_settings
        settings = get_settings()
        max_rows = settings.effective_db_max_rows
        # Dispatch to typed implementations
        result: DatabaseResult | None = None
        if tool_name in ("search_users", "search_customers"):
            result = await _execute_search_users(session, args, max_rows)
        elif tool_name in ("get_user", "get_customer"):
            result = await _execute_get_user(session, args, max_rows)
        elif tool_name == "list_conversations":
            result = await _execute_list_conversations(session, args, max_rows, caller_id=audit_user_id)
        elif tool_name == "get_conversation_messages":
            result = await _execute_get_conversation_messages(session, args, max_rows, caller_id=audit_user_id)
        else:
            raise DatabaseValidationError(f"Tool {tool_name!r} not implemented")
        log_db_tool_call(
            request_id=rid,
            user_id=audit_user_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=args,
            result_row_count=result.row_count,
            duration_ms=timer.stop(),
            success=True,
            database_object=tool_name,
        )
        return result
    except Exception as exc:
        from app.db.errors import DatabaseError
        if isinstance(exc, DatabaseError):
            sanitized = "timeout" if isinstance(getattr(exc, "cause", None), TimeoutError) or "timed out" in str(exc).lower() else "database_error"
            if sanitized == "timeout":
                log_db_tool_call(
                    request_id=rid,
                    user_id=audit_user_id,
                    conversation_id=conversation_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    duration_ms=timer.stop(),
                    success=False,
                    error="timeout",
                )
                raise
            # For validation/permission errors, log with sanitized code
            # Don't expose raw SQL or sensitive data
            err_val = sanitized if sanitized in ("timeout", "database_error") else str(exc)[:300]
            # For validation errors, include short message (safe)
            if isinstance(exc, DatabaseValidationError):
                err_val = str(exc)[:300]
            elif isinstance(exc, DatabasePermissionError):
                err_val = "permission_denied"
            log_db_tool_call(
                request_id=rid,
                user_id=audit_user_id,
                conversation_id=conversation_id,
                tool_name=tool_name,
                arguments=arguments,
                duration_ms=timer.stop(),
                success=False,
                error=err_val,
            )
        else:
            logger.error("tool_execution_failed tool=%s request_id=%s error=%s", tool_name, rid, exc, exc_info=exc)
            log_db_tool_call(
                request_id=rid,
                user_id=audit_user_id,
                conversation_id=conversation_id,
                tool_name=tool_name,
                arguments=arguments,
                duration_ms=timer.stop(),
                success=False,
                error=f"{type(exc).__name__}:{rid}",
            )
        raise
