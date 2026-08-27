import os

# Must set env before importing app
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["DATABASE_URL_SYNC"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret-key-for-unit-tests-32chars!!"
os.environ.setdefault("OLLAMA_NODE_1_URL", "http://node1.test:11434")
os.environ.setdefault("OLLAMA_NODE_1_MODEL", "qwen3:1.7b")
os.environ.setdefault("OLLAMA_NODE_1_ENABLED", "true")
os.environ.setdefault("OLLAMA_NODE_2_URL", "http://node2.test:11434")
os.environ.setdefault("OLLAMA_NODE_2_MODEL", "qwen3.5:0.8b")
os.environ.setdefault("OLLAMA_NODE_2_ENABLED", "true")
os.environ.setdefault("AI_DEFAULT_NODE", "node1")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import clear_settings_cache
from app.db.session import Base
from app.main import create_app


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest_asyncio.fixture
async def db_engine():
    # Create a fresh in-memory engine per test session (function scope)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def app_client(db_engine):
    app = create_app()
    # Override with test in-memory engine for isolation — dispose default and use fixture's engine
    if hasattr(app.state, "engine"):
        try:
            await app.state.engine.dispose()  # type: ignore[attr-defined]
        except Exception:
            pass
    app.state.engine = db_engine  # type: ignore[attr-defined]
    app.state.session_factory = async_sessionmaker(db_engine, expire_on_commit=False)  # type: ignore[attr-defined]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Expose app for DI-based router patching (app.state.router)
        client.app = app  # type: ignore[attr-defined]
        yield client

    # Clean up app.state without disposing db_engine (owned by db_engine fixture)
    if hasattr(app.state, "engine"):
        try:
            delattr(app.state, "engine")
        except AttributeError:
            pass
    if hasattr(app.state, "session_factory"):
        try:
            delattr(app.state, "session_factory")
        except AttributeError:
            pass


@pytest_asyncio.fixture
async def authed_client(app_client):
    # Register then use token
    import uuid

    username = f"user_{uuid.uuid4().hex[:6]}"
    r = await app_client.post("/api/auth/register", json={"username": username, "password": "pass123456789", "display_name": "Test"})
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    app_client.headers["Authorization"] = f"Bearer {token}"
    # Attach user info for convenience
    app_client._test_user = r.json()["user"]
    app_client._test_token = token
    return app_client
