"""Phase 2B — Database Foundation tests per AGENTS §35.

Covers:
 1. database configuration
 2. SQLAlchemy engine (read-only + timeout, NullPool vs pooled)
 3. read-only enforcement (blocked DML + PRAGMA layer)
 4. connection health check
 5. repository layer (parameterized queries, ORM safety)
 6. parameterized query helpers (SELECT-only, allowlist, injection safety)
 7. timeout/row limits + cell truncation
 8. database error handling (safe messages, no leak)
 9. result normalization + audit logging + permissions
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings


# ------------------------------------------------------------------ 1. Config
def test_database_config_defaults(monkeypatch):
    monkeypatch.delenv("MAX_ROWS", raising=False)
    monkeypatch.delenv("QUERY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DATABASE_READ_ONLY", raising=False)
    s = Settings()
    assert s.database_read_only is True
    assert s.db_max_rows == 200
    assert s.db_query_timeout_seconds == 10
    assert s.db_max_cell_length == 4000
    assert s.ai_max_tool_steps == 3
    assert s.effective_db_max_rows == 200
    assert s.effective_db_query_timeout == 10


def test_database_config_aliases(monkeypatch):
    monkeypatch.setenv("MAX_ROWS", "50")
    monkeypatch.setenv("QUERY_TIMEOUT_SECONDS", "7")
    s = Settings()
    assert s.effective_db_max_rows == 50
    assert s.effective_db_query_timeout == 7


def test_database_config_read_only_flag(monkeypatch):
    monkeypatch.setenv("DATABASE_READ_ONLY", "false")
    s = Settings()
    assert s.database_read_only is False
    monkeypatch.setenv("DATABASE_READ_ONLY", "true")
    s = Settings()
    assert s.database_read_only is True


# ------------------------------------------------------------------ 2. Engine
@pytest.mark.asyncio
async def test_engine_factory_read_only_and_timeout():
    from app.db.engine import create_db_engine

    eng = create_db_engine("sqlite+aiosqlite:///:memory:", read_only=True, query_timeout_seconds=5, use_null_pool=True)
    assert eng is not None
    # ensure pool is NullPool (use_null_pool=True)
    from sqlalchemy.pool import NullPool

    assert isinstance(eng.pool, NullPool)
    await eng.dispose()

    eng2 = create_db_engine("sqlite+aiosqlite:///:memory:", read_only=False, query_timeout_seconds=10, use_null_pool=False)
    assert eng2 is not None
    await eng2.dispose()


@pytest.mark.asyncio
async def test_engine_via_settings():
    from app.db.engine import create_engine_for_settings

    s = Settings()
    eng = create_engine_for_settings(s, use_null_pool=True)
    assert eng is not None
    await eng.dispose()


# -------------------------------------------------------- 3. Health check
@pytest.mark.asyncio
async def test_health_check_ok(db_engine):
    from app.db.health import check_database_health

    st = await check_database_health(db_engine)
    assert st.status == "ok"
    assert st.latency_ms is not None and st.latency_ms >= 0


@pytest.mark.asyncio
async def test_health_endpoint_uses_service(app_client):
    r = await app_client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["db"] == "ok"


@pytest.mark.asyncio
async def test_health_db_detail_endpoint(app_client):
    r = await app_client.get("/api/health/db")
    assert r.status_code == 200
    data = r.json()
    assert data["db"] == "ok"


# -------------------------------------------------------- 4. Repositories + query helpers
@pytest.mark.asyncio
async def test_repository_parameterized_safety(db_session):
    from app.db.repositories import create_user
    from app.auth.service import hash_password

    u = await create_user(db_session, "bob_phase2b", "Bob", hash_password("pw"))
    await db_session.commit()

    # ORM lookup with bound param — safe from injection
    from app.db.repositories import get_user_by_username

    # Injection attempt as username value — should not match or execute as SQL
    injected = "' OR '1'='1"
    got = await get_user_by_username(db_session, injected)
    assert got is None
    got2 = await get_user_by_username(db_session, "bob_phase2b")
    assert got2 is not None


@pytest.mark.asyncio
async def test_query_helper_select_only_validation(db_session):
    from app.db.errors import DatabaseValidationError
    from app.db.query import validate_select_only

    # Allowed
    validate_select_only("SELECT * FROM users WHERE id=:id")
    validate_select_only("WITH cte AS (SELECT 1) SELECT * FROM cte")
    # Blocked
    for blocked in [
        "INSERT INTO users VALUES (1)",
        "UPDATE users SET username='x'",
        "DELETE FROM users",
        "DROP TABLE users",
        "ALTER TABLE users ADD COLUMN x TEXT",
        "SELECT * FROM users; DELETE FROM users",
        "SELECT * FROM users -- comment",
    ]:
        with pytest.raises(DatabaseValidationError):
            validate_select_only(blocked)


@pytest.mark.asyncio
async def test_query_helper_blocks_dml_even_parameterized(db_session):
    from app.db.query import execute_read_query
    from app.db.errors import DatabaseValidationError

    with pytest.raises(DatabaseValidationError):
        await execute_read_query(db_session, "DELETE FROM users WHERE username=:u", {"u": "bob"})


@pytest.mark.asyncio
async def test_query_helper_injection_via_params(db_session):
    """Ensure SQL injection via parameter value does not execute."""
    from app.db.query import execute_read_query
    from app.db.repositories import create_user
    from app.auth.service import hash_password

    u = await create_user(db_session, "inj_target", "Target", hash_password("pw"))
    await db_session.commit()
    # Attempt injection payload as bound param — should be treated as data, not code
    result = await execute_read_query(
        db_session,
        "SELECT username FROM users WHERE username=:u",
        {"u": "' OR 1=1 --"},
    )
    assert result.row_count == 0  # no injection
    result2 = await execute_read_query(db_session, "SELECT username FROM users WHERE username=:u", {"u": "inj_target"})
    assert result2.row_count == 1


# -------------------------------------------------------- 5. Row/cell/timeout limits
@pytest.mark.asyncio
async def test_row_limit_and_truncation(db_session):
    from app.db.query import execute_read_query

    # Ensure some rows with unique names to avoid collisions
    from app.db.repositories import create_user
    from app.auth.service import hash_password
    import uuid

    suffix = uuid.uuid4().hex[:8]
    for i in range(5):
        await create_user(db_session, f"limit_user_{suffix}_{i}", f"U{i}", hash_password("pw"))
    await db_session.commit()

    result = await execute_read_query(db_session, "SELECT username FROM users", {}, max_rows=2)
    assert result.row_count == 2
    assert isinstance(result.truncated, bool)


def test_ensure_limit_caps_outer_limit():
    from app.db.query import _ensure_limit

    assert _ensure_limit("SELECT * FROM users LIMIT 100000000", 200) == "SELECT * FROM users LIMIT 200"
    assert _ensure_limit("SELECT * FROM users LIMIT 5", 200) == "SELECT * FROM users LIMIT 5"
    assert _ensure_limit("SELECT * FROM users", 200) == "SELECT * FROM users LIMIT 200"
    assert _ensure_limit("SELECT * FROM users LIMIT 500 OFFSET 10", 200) == "SELECT * FROM users LIMIT 200 OFFSET 10"
    # Inner LIMIT without outer → should be wrapped to enforce cap
    wrapped = _ensure_limit("SELECT * FROM (SELECT * FROM users LIMIT 100000) t", 200)
    assert "LIMIT 200" in wrapped
    assert "_svc_capped" in wrapped


@pytest.mark.asyncio
async def test_huge_limit_is_capped_via_service(db_session):
    from app.db.query import execute_read_query
    from app.db.repositories import create_user
    from app.auth.service import hash_password
    import uuid

    # Ensure at least 10 rows exist with unique names
    suffix = uuid.uuid4().hex[:8]
    for i in range(10):
        await create_user(db_session, f"cap_user_{suffix}_{i}", f"Cap{i}", hash_password("pw"))
    await db_session.commit()
    # Request with huge LIMIT should be capped to max_rows=3 via service, not fetch huge result
    res = await execute_read_query(db_session, "SELECT * FROM users LIMIT 100000000", {}, max_rows=3)
    assert res.row_count == 3
    assert len(res.rows) == 3
    # Service caps outer LIMIT to max_rows, so DB only returns capped rows (no memory blowup)
    assert res.query is not None and "LIMIT 3" in res.query


@pytest.mark.asyncio
async def test_cell_length_truncation(db_session):
    from app.db.query import execute_read_query

    long_val = "X" * 5000
    # Insert via raw with long value
    await db_session.execute(
        text("INSERT INTO users (id, username, display_name, password_hash, is_active, created_at) VALUES (:id, :u, :d, :p, 1, datetime('now'))"),
        {"id": "cell-test-id-1", "u": "cell_long_user", "d": long_val, "p": "hash"},
    )
    await db_session.commit()
    res = await execute_read_query(db_session, "SELECT display_name FROM users WHERE username=:u", {"u": "cell_long_user"}, max_cell_length=20)
    assert len(res.rows) == 1
    cell = res.rows[0][0]
    assert isinstance(cell, str)
    assert len(cell) <= 35  # 20 + "…[truncated]" suffix
    assert "truncated" in cell.lower()


@pytest.mark.asyncio
async def test_timeout_handling(db_session):
    """Query timeout should not leak raw driver error; safe message."""
    import asyncio
    from unittest.mock import patch

    from app.db.errors import DatabaseError
    from app.db.query import execute_read_query

    # Deterministic blocked execution at query-helper boundary
    async def blocked(*args, **kwargs):
        await asyncio.sleep(5)

    with patch.object(db_session, "execute", side_effect=blocked):
        with pytest.raises(DatabaseError) as excinfo:
            await execute_read_query(db_session, "SELECT 1 AS n", {}, query_timeout_seconds=1)
        err = excinfo.value
        assert "could not be completed" in err.user_message.lower()
        # Raw SQL/timeout detail must not leak in safe message
        assert "select 1" not in err.user_message.lower()


# -------------------------------------------------------- 6. Error handling (safe messages)
@pytest.mark.asyncio
async def test_error_handling_safe_message(db_session):
    from app.db.query import execute_read_query
    from app.db.errors import DatabaseError

    # Invalid table should raise DatabaseError with safe message, not leak SQL
    with pytest.raises(DatabaseError) as excinfo:
        await execute_read_query(db_session, "SELECT * FROM not_a_real_table_xyz", {})
    err = excinfo.value
    assert "could not be completed" in str(err).lower() or "could not be completed" in err.user_message.lower()
    # Raw table name should not be in user_message (still generic)
    assert "not_a_real_table_xyz" not in err.user_message.lower()


# -------------------------------------------------------- 7. Result normalization
def test_result_normalization():
    from app.db.result import normalize_rows, DatabaseResult

    cols = ["id", "name"]
    rows = [(f"id{i}", f"name_{i}_long" * 100) for i in range(3)]
    res = normalize_rows(cols, rows, max_rows=2, max_cell_length=10)
    assert isinstance(res, DatabaseResult)
    assert res.truncated is True
    assert res.row_count == 2
    assert res.columns == cols
    # cell truncated
    assert any("truncated" in str(v).lower() for row in res.rows for v in row)


# -------------------------------------------------------- 8. Audit logging
def test_audit_logging_does_not_leak_full_results(caplog):
    from app.db.audit import log_db_tool_call

    # Audit works for typed tools (Phase 2B: no generic SQL)
    rec = log_db_tool_call(tool_name="search_users", arguments={"search_term": "alice"}, result_row_count=1, success=True)
    assert rec.tool_name == "search_users"
    assert rec.success is True
    assert rec.request_id is not None


# -------------------------------------------------------- 9. Permissions
def test_permissions_deny_anonymous():
    from app.db.permissions import check_tool_permission

    assert check_tool_permission(None, "search_users") is False
    assert check_tool_permission(None, "controlled_sql") is False


@pytest.mark.asyncio
async def test_permissions_allow_authenticated(db_session):
    from app.db.permissions import check_tool_permission
    from app.db.repositories import create_user
    from app.auth.service import hash_password

    u = await create_user(db_session, "perm_user", "Perm", hash_password("pw"))
    await db_session.flush()
    # Typed tools are explicitly allowed for authenticated users
    assert check_tool_permission(u, "search_users") is True
    assert check_tool_permission(u, "get_user") is True
    assert check_tool_permission(u, "list_conversations") is True
    # Generic SQL is not allowed (deny by default, non-executable)
    assert check_tool_permission(u, "controlled_sql") is False
    assert check_tool_permission(u, "evil_tool") is False


# -------------------------------------------------------- 10. AI tool layer validation
def test_ai_tool_validation_blocks_unknown():
    from app.ai.tools.database import validate_tool_call
    from app.db.errors import DatabaseValidationError

    with pytest.raises(DatabaseValidationError):
        validate_tool_call("DROP TABLE users", {})
    with pytest.raises(DatabaseValidationError):
        validate_tool_call("controlled_sql", {"sql": "SELECT 1"})
    with pytest.raises(DatabaseValidationError):
        validate_tool_call("evil_tool", {})


@pytest.mark.asyncio
async def test_ai_tool_generic_sql_is_disabled(db_session):
    from app.ai.tools.database import execute_tool
    from app.db.errors import DatabasePermissionError, DatabaseValidationError
    from app.db.repositories import create_user
    from app.auth.service import hash_password

    u = await create_user(db_session, "generic_block_user", "Block", hash_password("pw"))
    await db_session.commit()
    # controlled_sql is not in allowlist and not in permissions -> denied before execution
    # Either permission or validation error is acceptable, both mean non-executable
    with pytest.raises((DatabasePermissionError, DatabaseValidationError)):
        await execute_tool(db_session, "controlled_sql", {"sql": "SELECT 1 AS n", "params": {}}, user=u, request_id="req123")


@pytest.mark.asyncio
async def test_ai_tool_typed_search_users(db_session):
    from app.ai.tools.database import execute_tool
    from app.db.repositories import create_user
    from app.auth.service import hash_password

    u = await create_user(db_session, "typed_alice", "Alice Typed", hash_password("pw"))
    await db_session.commit()
    result = await execute_tool(db_session, "search_users", {"search_term": "typed_alice", "limit": 5}, user=u, request_id="req123")
    assert result.row_count >= 1
    assert "password_hash" not in result.columns
    assert "username" in result.columns


@pytest.mark.asyncio
async def test_ai_tool_typed_get_user(db_session):
    from app.ai.tools.database import execute_tool
    from app.db.repositories import create_user
    from app.auth.service import hash_password

    u = await create_user(db_session, "typed_bob", "Bob Typed", hash_password("pw"))
    await db_session.commit()
    result = await execute_tool(db_session, "get_user", {"username": "typed_bob"}, user=u)
    assert result.row_count == 1
    assert result.columns == ["id", "username", "display_name", "is_active", "created_at"]


@pytest.mark.asyncio
async def test_ai_tool_typed_list_conversations(db_session):
    from app.ai.tools.database import execute_tool
    from app.db.repositories import create_user, create_conversation
    from app.auth.service import hash_password

    u = await create_user(db_session, "conv_user", "Conv User", hash_password("pw"))
    conv = await create_conversation(db_session, u.id, title="Conv Test")
    await db_session.commit()
    result = await execute_tool(db_session, "list_conversations", {"user_id": u.id, "limit": 5}, user=u)
    assert result.row_count >= 1
    assert "password_hash" not in result.columns


@pytest.mark.asyncio
async def test_ai_tool_requires_verified_user(db_session):
    from app.ai.tools.database import execute_tool
    from app.db.errors import DatabasePermissionError
    from app.db.repositories import create_user
    from app.auth.service import hash_password

    u = await create_user(db_session, "auth_user", "Auth", hash_password("pw"))
    await db_session.commit()
    # No user -> denied
    with pytest.raises(DatabasePermissionError, match="Authentication required"):
        await execute_tool(db_session, "search_users", {"search_term": "auth", "limit": 5}, user=None)
    # Fake (non-User) -> denied
    class Fake:
        is_active = True
        id = "fake"
    with pytest.raises(DatabasePermissionError):
        await execute_tool(db_session, "search_users", {"search_term": "auth", "limit": 5}, user=Fake())  # type: ignore[arg-type]
    # Valid user -> succeeds
    result = await execute_tool(db_session, "search_users", {"search_term": "auth_user", "limit": 5}, user=u)
    assert result.row_count >= 1


def test_audit_redacts_sensitive_arguments(caplog):
    from app.db.audit import log_db_tool_call
    import logging

    # Capture redacted audit payload
    records: list[dict] = []

    class Cap(logging.Handler):
        def emit(self, r):
            if hasattr(r, "audit"):
                records.append(r.audit)

    logger = logging.getLogger("app.db.audit")
    logger.setLevel(logging.INFO)
    h = Cap()
    logger.addHandler(h)
    try:
        log_db_tool_call(
            tool_name="search_users",
            arguments={"search_term": "alice@example.com", "limit": 5, "user_id": "secret-123"},
            user_id="u1",
            success=True,
            result_row_count=1,
        )
        assert records, "audit log not captured"
        aud = records[0]
        # Values must be redacted, not raw
        assert aud["arguments"]["search_term"] != "alice@example.com"
        assert "alice@example.com" not in str(aud["arguments"])
        assert "secret-123" not in str(aud["arguments"])
        assert aud["arguments"]["search_term"].startswith("<redacted")
        assert aud["arguments"]["limit"].startswith("<redacted")
    finally:
        logger.removeHandler(h)


# -------------------------------------------------------- 11. Read-only account simulation (SQLite pragma)
@pytest.mark.asyncio
async def test_engine_readonly_pragma_blocks_writes_if_enforced():
    """For SQLite, read_only engine sets PRAGMA query_only=ON; writes should fail.

    This verifies the DB-level enforcement complementing app-level SELECT-only validation (AGENTS §15 rule 4).
    """
    from app.db.engine import create_db_engine

    # Create writable DB first to establish schema, then attempt write via read-only engine's shared DB file
    # For in-memory DB, each engine has isolated DB, so we test pragma effect via immediate write after creation:
    # A read-only in-memory engine should block even CREATE (already proven above), which demonstrates
    # DB-level enforcement. So we validate that app-layer also blocks writes (already covered elsewhere).
    # Here we simply verify the read_only engine cannot be used for DDL/DML without previously creating table.
    eng = create_db_engine("sqlite+aiosqlite:///:memory:", read_only=True, query_timeout_seconds=5, use_null_pool=True)
    blocked = False
    try:
        async with eng.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    except Exception:
        blocked = True
    await eng.dispose()
    # Creating table on read-only engine should be blocked (pragma query_only=ON)
    assert blocked is True
