from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conv_router
from app.api.health import router as health_router
from app.config import get_settings
from app.db.session import init_db

# Structured logging per AGENTS §16
def setup_logging() -> None:
    import json
    from typing import Any

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    class StructuredFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            data: dict[str, Any] = {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            for field in ("method", "path", "status_code", "latency_ms", "request_id"):
                if hasattr(record, field):
                    data[field] = getattr(record, field)
            return json.dumps(data, ensure_ascii=False)

    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Ensure lifespan-scoped settings, health_manager, router and DB resources are available on app.state
    from app.ai.health import HealthManager
    from app.ai.router import AIRouter
    from app.config import Settings
    from app.db.session import Base, _create_engine_for_app
    from sqlalchemy.ext.asyncio import async_sessionmaker

    if not hasattr(app.state, "health_manager"):
        app.state.health_manager = HealthManager()  # type: ignore[attr-defined]
    if not hasattr(app.state, "settings"):
        app.state.settings = Settings()  # type: ignore[attr-defined]
    if not hasattr(app.state, "router"):
        app.state.router = AIRouter(app.state.settings, app.state.health_manager)  # type: ignore[attr-defined]
    if not hasattr(app.state, "engine"):
        app.state.engine = _create_engine_for_app(app)  # type: ignore[attr-defined]
    if not hasattr(app.state, "session_factory"):
        app.state.session_factory = async_sessionmaker(app.state.engine, expire_on_commit=False)  # type: ignore[attr-defined]
    setup_logging()
    await init_db(app)
    try:
        yield
    finally:
        # Dispose engine on shutdown and remove from lifecycle state
        if hasattr(app.state, "engine"):
            try:
                await app.state.engine.dispose()  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                delattr(app.state, "engine")
            except AttributeError:
                pass
        if hasattr(app.state, "session_factory"):
            try:
                delattr(app.state, "session_factory")
            except AttributeError:
                pass


def create_app() -> FastAPI:
    from app.ai.health import HealthManager
    from app.ai.router import AIRouter
    from app.config import Settings
    from app.db.session import _create_engine_for_app
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings = Settings()  # type: ignore[call-arg]
    health_manager = HealthManager()
    app = FastAPI(title="Nexus.ai Office AI", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings  # type: ignore[attr-defined]
    app.state.health_manager = health_manager  # type: ignore[attr-defined]
    app.state.engine = _create_engine_for_app(app)  # type: ignore[attr-defined]
    app.state.session_factory = async_sessionmaker(app.state.engine, expire_on_commit=False)  # type: ignore[attr-defined]
    # Eager router for test clients that don't trigger lifespan, lifespan will reuse if already present
    app.state.router = AIRouter(settings, health_manager)  # type: ignore[attr-defined]

    # CORS — explicit origins from settings; wildcard with credentials is insecure
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_logging(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        start = time.perf_counter()
        response = await call_next(request)
        latency_ms = int((time.perf_counter() - start) * 1000)
        log = logging.getLogger("app.requests")
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "request_id": request_id,
            },
        )
        response.headers["X-Request-ID"] = request_id
        return response

    # Routers
    app.include_router(auth_router)
    app.include_router(conv_router)
    app.include_router(chat_router)
    app.include_router(health_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"name": "Nexus.ai", "status": "ok", "env": settings.app_env, "docs": "/docs"}

    # Serve static frontend (public/app) at /app — keep agnostic to TanStack build
    import pathlib

    static_dir = pathlib.Path(__file__).resolve().parent.parent / "public" / "app"
    if static_dir.exists():
        app.mount("/app", StaticFiles(directory=str(static_dir), html=True), name="static-app")

    # Also serve public root static (favicon etc) at /static
    public_dir = pathlib.Path(__file__).resolve().parent.parent / "public"
    if public_dir.exists():
        app.mount("/static", StaticFiles(directory=str(public_dir)), name="public-static")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger(__name__).exception("unhandled_error path=%s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()
