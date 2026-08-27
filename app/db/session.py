from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import get_settings

# Process-scoped engine for callers without an explicit FastAPI app.
# Reused across calls to avoid creating unmanaged pooled engines repeatedly;
# explicitly disposed via shutdown_process_engine() or ephemeral context.
_process_engine: AsyncEngine | None = None
_process_session_factory: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    pass


def _create_engine_for_app(app) -> AsyncEngine:
    # Use app.state.settings if available, otherwise fallback to get_settings()
    try:
        settings = app.state.settings  # type: ignore[attr-defined]
    except AttributeError:
        settings = get_settings()
    engine: AsyncEngine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
    )
    return engine


def _get_process_engine() -> AsyncEngine:
    """Return process-scoped engine for no-app callers.

    Uses NullPool and is explicitly owned; callers must not dispose directly —
    use shutdown_process_engine() or ephemeral_engine() for explicit lifecycle.
    Reused across calls to avoid unmanaged pooled engines.
    """
    global _process_engine
    if _process_engine is not None:
        return _process_engine
    settings = get_settings()
    _process_engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        poolclass=NullPool,
    )
    return _process_engine


async def shutdown_process_engine() -> None:
    """Dispose the process-scoped engine/factory created for no-app usage."""
    global _process_engine, _process_session_factory
    if _process_engine is not None:
        await _process_engine.dispose()
        _process_engine = None
    _process_session_factory = None


@asynccontextmanager
async def ephemeral_engine() -> AsyncIterator[AsyncEngine]:
    """Async context manager for explicitly owned ephemeral engines.

    Prefer this when no FastAPI app lifecycle is available — guarantees disposal.
    Uses NullPool to avoid pooled connections.
    """
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        poolclass=NullPool,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def ephemeral_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Ephemeral session factory with explicit disposal of its engine."""
    async with ephemeral_engine() as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        yield factory


def get_engine(app: FastAPI | None = None) -> AsyncEngine:
    """Get engine from app.state if app provided, otherwise reuse process-scoped engine.

    Per-app engines are stored on app.state and created via _create_engine_for_app
    so separate FastAPI instances never share or reuse a disposed engine.
    The no-app path reuses a process-scoped NullPool engine with explicit
    shutdown via shutdown_process_engine() — no unmanaged pooled engine per call.
    """
    if app is not None:
        if hasattr(app.state, "engine"):
            return app.state.engine  # type: ignore[attr-defined]
        engine = _create_engine_for_app(app)
        app.state.engine = engine  # type: ignore[attr-defined]
        return engine
    # No app — reuse process-scoped engine with explicit lifecycle
    return _get_process_engine()


def get_session_factory(app: FastAPI | None = None) -> async_sessionmaker[AsyncSession]:
    """Get session factory from app.state if app provided, otherwise process-scoped."""
    if app is not None:
        if hasattr(app.state, "session_factory"):
            return app.state.session_factory  # type: ignore[attr-defined]
        engine = get_engine(app)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        app.state.session_factory = factory  # type: ignore[attr-defined]
        return factory
    global _process_session_factory
    if _process_session_factory is not None:
        return _process_session_factory
    engine = get_engine(None)
    _process_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return _process_session_factory


async def get_db(request: Request):
    """FastAPI dependency — yields an AsyncSession from app.state."""
    # Prefer app.state.session_factory if available; otherwise initialize per-app via _create_engine_for_app
    try:
        factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory  # type: ignore[attr-defined]
    except AttributeError:
        # Use per-app factory initialization (ensures app.state.engine/session_factory are set)
        factory = get_session_factory(request.app)
    async with factory() as session:
        yield session


async def init_db(app=None) -> None:
    """Create all tables — for Phase 1 we use create_all; Alembic can be added later."""
    from app.db import models  # noqa: F401 — ensure models imported

    if app is not None:
        # Per-app engine — owned by app lifecycle (disposed via lifespan/reset_engine)
        if hasattr(app.state, "engine"):
            engine = app.state.engine  # type: ignore[attr-defined]
        else:
            engine = get_engine(app)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return
    # No app — explicitly owned ephemeral engine with guaranteed disposal
    async with ephemeral_engine() as engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


async def reset_engine(app: FastAPI | None = None) -> None:
    """Dispose and remove engine/session_factory from app.state if present.

    Only the specified app's state is cleared so separate FastAPI instances
    never share or reuse disposed engines. If app is None, disposes the
    process-scoped engine (explicit lifecycle).
    """
    if app is not None and hasattr(app.state, "engine"):
        engine: AsyncEngine = app.state.engine  # type: ignore[attr-defined]
        await engine.dispose()
        delattr(app.state, "engine")
        if hasattr(app.state, "session_factory"):
            delattr(app.state, "session_factory")
        return
    if app is None:
        await shutdown_process_engine()
        return
    return None
