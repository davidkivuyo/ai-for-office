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
