"""Simple health check for both Ollama nodes and the API."""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

API_BASE = os.getenv("API_BASE", "http://localhost:8000")


def _load_nodes() -> list[tuple[str, str]]:
    from app.config import get_settings

    settings = get_settings()
    nodes = settings.ollama_nodes()
    valid = [(n.url, n.model) for n in nodes if n.enabled and n.url and n.model]
    if not valid:
        print("ERROR: no Ollama nodes configured — check OLLAMA_NODE_* env vars", file=sys.stderr)
        sys.exit(1)
    return valid


async def check_api():
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            r = await c.get(f"{API_BASE}/api/health")
            print(f"API /api/health: {r.status_code} {r.text[:300]}")
            r2 = await c.get(f"{API_BASE}/api/nodes/health")
            print(f"API /api/nodes/health: {r2.status_code} {r2.text[:800]}")
            return r.is_success and r2.is_success
        except Exception as e:
            print(f"API check failed: {e}")
            return False


async def check_node(url: str, model: str):
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            r = await c.get(f"{url.rstrip('/')}/api/tags")
            ok = r.status_code == 200
            models = [n for n in (m.get("name") for m in r.json().get("models", [])) if n] if ok else []
            has = any(model in m or m.startswith(model) for m in models)
            print(f"Node {url} model={model}: {'OK' if ok and has else 'DEGRADED'} tags={r.status_code} models={models[:5]} has_model={has}")
            return ok and has
        except Exception as e:
            print(f"Node {url} failed: {e}")
            return False


async def main():
    print("=== Nexus.ai healthcheck ===")
    nodes = _load_nodes()
    api_ok = await check_api()
    node_oks = await asyncio.gather(*(check_node(u, m) for u, m in nodes))
    if api_ok and all(node_oks):
        print("All healthy")
        sys.exit(0)
    elif api_ok and any(node_oks):
        print("API ok, some nodes degraded — check logs")
        sys.exit(0)
    elif api_ok:
        print("API ok, no nodes healthy — check logs")
        sys.exit(1)
    else:
        print("API not reachable")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
