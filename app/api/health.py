from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.health import HealthManager, get_health_manager_dep
from app.auth.permissions import get_current_user_optional
from app.config import Settings, get_settings_dep
from app.db.models import User
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings_dep)) -> dict[str, str]:
    # DB check
    db_status = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health_db_check_failed")
        db_status = "error"

    return {"status": "ok" if db_status == "ok" else "degraded", "db": db_status, "env": settings.app_env}


@router.get("/nodes/health")
async def nodes_health(
    settings: Settings = Depends(get_settings_dep),
    manager: HealthManager = Depends(get_health_manager_dep),
    current: User | None = Depends(get_current_user_optional),
) -> dict[str, Any]:
    # Reuse cached states if recently fresh (regardless of status) to prevent
    # each request from triggering concurrent provider probes;
    # in-flight checks are coalesced per node in HealthManager.
    cached = manager.all()
    now = time.time()
    if cached and all(s.last_checked is not None and (now - s.last_checked) < manager.cooldown for s in cached):
        states = cached
    else:
        states = await manager.check_all()
    out = []
    for st in states:
        if current is not None:
            # Authenticated: expose model for UI (admin) while still hiding sensitive detail/errors
            out.append(
                {
                    "node_id": st.node_id,
                    "status": st.status.value,
                    "model": st.model,
                }
            )
        else:
            # Unauthenticated: minimal exposure per security finding
            out.append(
                {
                    "node_id": st.node_id,
                    "status": st.status.value,
                }
            )
    return {"nodes": out, "default_node": settings.ai_default_node, "fallback_enabled": settings.ai_fallback_enabled}
