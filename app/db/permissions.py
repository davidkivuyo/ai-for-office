"""User permission checks for database tools per AGENTS §23/§24.

Flow:
  authenticated user -> role/department -> allowed tools -> allowed objects

Never rely on model to decide permissions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.db.models import User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolPermission:
    tool_name: str
    allowed_roles: set[str] | None = None  # None = all authenticated
    allowed_tables: set[str] | None = None
    allowed_columns: set[str] | None = None
    max_rows: int | None = None


# Default allowlist for Phase 2B — explicit typed tools only per AGENTS §17
# Deny by default: tools must be explicitly registered before they become executable
# per "Never rely on the model to decide permissions."
DEFAULT_PERMISSIONS: dict[str, ToolPermission] = {
    # Typed domain tools — repository-backed, no generic SQL (AGENTS §17)
    "search_users": ToolPermission(
        tool_name="search_users",
        allowed_tables={"users"},
        allowed_columns={"id", "username", "display_name", "is_active", "created_at"},
        max_rows=50,
    ),
    "search_customers": ToolPermission(
        tool_name="search_customers",
        allowed_tables={"users"},
        allowed_columns={"id", "username", "display_name", "is_active", "created_at"},
        max_rows=50,
    ),
    "get_user": ToolPermission(
        tool_name="get_user",
        allowed_tables={"users"},
        allowed_columns={"id", "username", "display_name", "is_active", "created_at"},
        max_rows=1,
    ),
    "get_customer": ToolPermission(
        tool_name="get_customer",
        allowed_tables={"users"},
        allowed_columns={"id", "username", "display_name", "is_active", "created_at"},
        max_rows=1,
    ),
    "list_conversations": ToolPermission(
        tool_name="list_conversations",
        allowed_tables={"conversations"},
        allowed_columns={"id", "user_id", "title", "created_at", "updated_at"},
        max_rows=50,
    ),
    "get_conversation_messages": ToolPermission(
        tool_name="get_conversation_messages",
        allowed_tables={"messages"},
        allowed_columns={"id", "conversation_id", "role", "content", "created_at"},
        max_rows=100,
    ),
}


def check_tool_permission(user: User | None, tool_name: str) -> bool:
    """Check if user is allowed to invoke tool_name.

    Secure default: deny when no permission record exists. Tools must be
    explicitly registered via DEFAULT_PERMISSIONS / register_permission()
    before they become executable (AGENTS §23, "Never rely on the model").
    """
    if user is None or not user.is_active:
        logger.warning("permission_denied tool=%s reason=no_user_or_inactive", tool_name)
        return False
    perm = DEFAULT_PERMISSIONS.get(tool_name)
    if perm is None:
        # Deny unregistered tools by default — explicit allowlist required
        logger.warning("permission_denied tool=%s reason=unregistered", tool_name)
        return False
    if perm.allowed_roles is None:
        return True
    if not perm.allowed_roles:
        return True
    # Fail-closed: non-empty role restriction configured but no trusted role source
    # Until User.role verification exists, deny to prevent bypass via register_permission
    logger.warning("permission_denied tool=%s reason=role_verification_unavailable", tool_name)
    return False


def assert_tool_permission(user: User | None, tool_name: str) -> None:
    """Raise DatabasePermissionError if not allowed."""
    from app.db.errors import DatabasePermissionError

    if not check_tool_permission(user, tool_name):
        raise DatabasePermissionError(
            f"Permission denied for tool {tool_name!r}",
        )


def filter_columns(tool_name: str, columns: list[str]) -> list[str]:
    """Per §24: drop sensitive columns not allowed for tool."""
    perm = DEFAULT_PERMISSIONS.get(tool_name)
    if perm is None or perm.allowed_columns is None:
        return columns
    return [c for c in columns if c in perm.allowed_columns]


def register_permission(permission: ToolPermission) -> None:
    """Register a new tool permission (used by ai/tools/database.py in Phase 2C)."""
    DEFAULT_PERMISSIONS[permission.tool_name] = permission
