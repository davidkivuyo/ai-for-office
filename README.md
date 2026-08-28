# Nexus.ai — Office AI Test Environment

Local, low-cost AI for office workers: chat with on-prem Ollama, store conversations, and keep the inference layer replaceable.

## Quick start (local)

```bash
# 1. Env
cp .env.example .env
# edit OLLAMA_NODE_1_URL / OLLAMA_NODE_2_URL to your LAN addresses
# defaults already match AGENTS.md §6 example; SECRET_KEY must be random in production

# 2. Python backend (3.12+)
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

see [GUIDES.md](docs/GUIDE.md)

## Configuration

All in `.env` — never commit real IPs/secrets. See `.env.example` for full list. Key vars:

```
OLLAMA_NODE_1_URL / _MODEL / _ENABLED
OLLAMA_NODE_2_URL / _MODEL / _ENABLED
OLLAMA_NODE_3_URL ...          # future GPU nodes, no code change (AGENTS §20)
AI_DEFAULT_NODE=node1
AI_FALLBACK_ENABLED=true
AI_TIMEOUT_SECONDS=120
AI_MAX_OUTPUT_TOKENS=1024
AI_MAX_CONTEXT_TOKENS=8192
AI_MAX_CONCURRENT_REQUESTS_PER_NODE=1
DATABASE_URL=sqlite+aiosqlite:///./nexus.db
SECRET_KEY=...
```

## Architecture

```
Browser  ->  FastAPI (app/main.py)
                |-- /api/auth, /api/conversations, /api/chat, /api/nodes/health
                |-- AIProvider (app/ai/provider.py) -> OllamaProvider (httpx) -> Ollama Node 1 / Node 2
                |-- Router (app/ai/router.py): explicit / round-robin / fallback
                |-- HealthManager (app/ai/health.py): healthy/degraded/offline/disabled
                |-- DB: users, conversations, messages (SQLAlchemy)
                \-- Static: public/app/* mounted at /app
```

# License

Licensed under [MIT](LICENSE)