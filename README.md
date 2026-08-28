# Nexus.ai — Office AI Test Environment

Local, low-cost AI for office workers: chat with on-prem Ollama, store conversations, and keep the inference layer replaceable.

Per `AGENTS.md` Phase 1:
- Two Ollama laptops as independent inference nodes (`qwen3:1.7b` / `qwen3.5:0.8b` — configurable via env)
- FastAPI + SQLAlchemy (SQLite) + async httpx provider + router with fallback
- JWT auth for local testing, conversation/message persistence with audit metadata (requested/actual model/node/latency)
- Node health checking, streaming, timeout & concurrency guards (1 req/node default for 12 GB laptops)
- Frontend is a static chat UI at `public/app/*` wired to `/api/*` (never talks to Ollama directly)

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

# generate token for auth
# 32 random bytes -> hex (64 chars) — use any generator
openssl rand -hex 32

# Open http://localhost:8000/docs  (Swagger) and http://localhost:8000/app/

# 3. Frontend dev (TanStack + Vite) — optional; proxies /api to :8000
npm i
npm run dev
# Vite at http://localhost:5173 proxies /api -> http://localhost:8000
```

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

## API

- `POST /api/auth/register` `{username,password,display_name}` -> token
- `POST /api/auth/login` -> token
- `GET  /api/auth/me` (Bearer)
- `GET/POST /api/conversations` , `GET/DELETE /api/conversations/{id}`
- `POST /api/chat` `{conversation_id?, message, node_id?, stream:false}` -> `{reply, actual_node/model, latency_ms}` (persists both messages, logs audit fields)
- `POST /api/chat/stream` SSE `data: {"token":...}` -> `data: [DONE]` (also persists)
- `GET  /api/health` , `GET /api/nodes/health`

All error paths return structured `{detail}` and never leak credentials or Ollama internals to the browser.

## Scripts

```bash
python scripts/healthcheck.py          # checks API + both nodes
python scripts/benchmark_models.py --api http://localhost:8000 --token $NEXUS_TOKEN
# outputs benchmark_results.csv; fill human_quality_score manually
```

## Tests

```bash
pytest -v
# 23 tests: config, auth, repositories, ollama provider (mocked), router (fallback/round-robin/truncation), chat API (persistence/isolation/401/502), health
```

Manual acceptance per AGENTS §21: login → start conversation → send prompt to each model → verify persistence → restart node → verify fallback → reconnect → health healthy.

## Docker

```bash
docker compose up --build
```

## Notes & limits (Phase 1)

- No RAG, no unrestricted SQL, no Kubernetes, no model sharding (AGENTS §9, §18).
- Moderate context/output limits; 1 concurrent request per node by default; reduce if laptop unstable.
- SQLite via `create_all` for Phase 1; Alembic can be added without changing chat logic.
- Logging is audit-friendly (request_id, user_id, node_id, latency_ms) but does not log message content unless `LOG_SENSITIVE_CONTENT=true`.

## killing processes
lsof -i :8000, pkill -f "uvicorn app.main:app"
