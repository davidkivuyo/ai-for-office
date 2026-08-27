from __future__ import annotations

from enum import Enum


class NodeHealth(str, Enum):
    healthy = "healthy"
    degraded = "degraded"
    offline = "offline"
    disabled = "disabled"
