# AGENTS.md — Phase 1 Office AI Test Environment

## 1. Purpose

This repository is the first test implementation of the Office AI platform.

The system is intended to help office workers:

- chat with a local AI model
- summarize and rewrite text
- create basic Word/Excel files
- store conversations
- later connect to approved company data sources
- later support document search/RAG
- later scale from laptop inference to dedicated GPU servers

Phase 1 is a **local, low-cost test environment**. Optimize for simplicity, observability, safety, and easy replacement of the inference layer.

Do not prematurely introduce Kubernetes, distributed model execution, microservices, or cloud dependencies.

---

## 2. Phase 1 Deployment Target

The test environment has **two laptops**, each with:

- at least 12 GB system RAM
- integrated GPU
- local network connectivity
- Ollama installed locally

Use the laptops as **independent inference nodes**.

### Node 1

Role: primary test inference node

Default model:

```text
qwen3:1.7b
```

### Node 2

Role: second inference node / model comparison

Default model:

```text
qwen3.5:0.8b
```

The purpose is to compare model quality, latency, RAM usage, and usefulness for office tasks.

Do **not** attempt to split a single model across the two laptops.

Do **not** make laptop 1 depend on laptop 2 for inference.

Each node must be independently usable.

---

## 3. Model Selection Notes

As of 2026-08-26, Ollama lists:

- `qwen3:1.7b` as a Qwen 3 model.
- `qwen3.5:0.8b` as a Qwen 3.5 model.

The Ollama library currently reports approximately:

- `qwen3:1.7b`: 7.2 GB package size
- `qwen3.5:0.8b`: 2.7 GB package size

These sizes are only the model package sizes. Runtime memory also depends on context length, KV cache, concurrency, and backend.

Because each laptop has only 12 GB RAM, keep Phase 1 conservative.

Use:

- one inference request at a time per node by default
- moderate context limits
- short-to-medium output limits
- no automatic parallel generation
- no large background indexing jobs while chatting

Model names must be configurable through environment variables. Never hard-code a model name throughout the application.

---

## 4. Core Architecture

The application should use this architecture:

```text
                     Office AI Web App
                           |
                           v
                  Python API / Backend
                           |
                     AI Provider API
                           |
                    +------+------+
                    |             |
                    v             v
              Ollama Node 1   Ollama Node 2
              qwen3:1.7b       qwen3.5:0.8b
              Laptop 1         Laptop 2
```

The application must treat the Ollama nodes as interchangeable providers.

Do not couple business logic directly to Ollama HTTP calls.

Use an abstraction such as:

```text
AIProvider
  |
  +-- OllamaProvider
```

The provider should receive:

- model
- messages
- temperature/options
- streaming preference
- timeout

and return a normalized application-level response.

---

## 5. Inference Strategy

### Phase 1 rule

Use **request routing**, not distributed inference.

A request may go to:

```text
Node 1 -> qwen3:1.7b
```

or:

```text
Node 2 -> qwen3.5:0.8b
```

The router may use:

1. explicit model selection for testing
2. round-robin routing
3. fallback routing when a node is unavailable

Do not implement GPU load balancing yet.

Do not implement model sharding across laptops.

Do not assume that an integrated GPU is actually being used.

The application must remain functional in CPU-only mode.

---

## 6. Required Configuration

Use environment variables.

Example:

```env
APP_ENV=development

OLLAMA_NODE_1_URL=http://192.168.1.101:11434
OLLAMA_NODE_1_MODEL=qwen3:1.7b
OLLAMA_NODE_1_ENABLED=true

OLLAMA_NODE_2_URL=http://192.168.1.102:11434
OLLAMA_NODE_2_MODEL=qwen3.5:0.8b
OLLAMA_NODE_2_ENABLED=true

AI_DEFAULT_NODE=node1

AI_TIMEOUT_SECONDS=120
AI_MAX_OUTPUT_TOKENS=1024
AI_MAX_CONTEXT_TOKENS=8192
AI_MAX_CONCURRENT_REQUESTS_PER_NODE=1
```

Never commit real IP addresses, passwords, API keys, or secrets to Git.

Keep `.env` in `.gitignore`.

Provide `.env.example`.

---

## 7. Network Requirements

Both Ollama laptops must be reachable from the application host over the office test LAN.

Recommended arrangement:

```text
Laptop 1: 192.168.1.101
Laptop 2: 192.168.1.102
Application host: 192.168.1.100
```

These are examples only. Do not hard-code them.

Ollama should listen on the required LAN interface only.

Do not expose Ollama directly to the public Internet.

For testing, allow only the application host to access Ollama where the operating system firewall permits it.

The browser/client should call the Python application, not Ollama directly.

---

## 8. Security Rules

The Python backend is the security boundary.

The browser must never receive direct database credentials.

The browser must never be given unrestricted access to Ollama.

The LLM must never receive unrestricted SQL execution privileges.

Do not implement a generic tool such as:

```text
execute_any_sql()
```

Instead create narrow, validated tools later, for example:

```text
get_customer()
get_sales_summary()
get_inventory_status()
```

All future write operations must require explicit backend permission checks.

For destructive or sensitive actions, add an approval/confirmation step.

---

## 9. Phase 1 Scope

### Required

Implement:

- login/authentication suitable for local testing
- user record
- conversation creation
- message storage
- basic chat UI
- Ollama provider
- two-node inference configuration
- model selection for testing
- streaming responses if practical
- timeout handling
- node health checking
- basic logging
- audit-friendly request metadata
- simple error messages

### Nice to have

- model comparison page
- response latency display
- tokens/second display when available
- node status page
- conversation export

### Not in Phase 1

Do not implement yet:

- company database write access
- autonomous agents
- unrestricted SQL
- document ingestion pipelines
- large RAG indexing
- Kubernetes
- distributed model inference
- GPU clustering
- cloud inference
- multi-tenant billing
- automatic model downloading
- automatic model switching based only on model output

---

## 10. Recommended Repository Structure

```text
nexus-ai-source/
├── AGENTS.md
├── README.md
|-- .gitignore
├── .env.example
├── .gitignore
├── pyproject.toml
├── docker-compose.yml
│
├── app/
│   ├── main.py
│   │
│   ├── api/
│   │   ├── auth.py
│   │   ├── chat.py
│   │   ├── conversations.py
│   │   └── health.py
│   │
│   ├── ai/
│   │   ├── provider.py
│   │   ├── ollama.py
│   │   ├── router.py
│   │   ├── models.py
│   │   └── health.py
│   │
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── repositories.py
│   │
│   ├── auth/
│   │   ├── service.py
│   │   └── permissions.py
│   │
│   └── schemas/
│       ├── chat.py
│       ├── user.py
│       └── conversation.py
│
├── tests/
│   ├── test_chat.py
│   ├── test_router.py
│   ├── test_ollama.py
│   └── test_auth.py
│
└── scripts/
|    ├── healthcheck.py
|    └── benchmark_models.py
|
|--- public/
|     └── index.html
|--- src/
|     └── frontend/
|      ├── components/
|      ├── pages/
|      └── styles/
```

---

## 11. Python Design Rules

Use:

- Python 3.12+ unless project compatibility requires otherwise
- FastAPI for the HTTP API
- Pydantic for request/config validation
- SQLAlchemy for persistence
- Alembic for migrations
- `httpx` or the official Ollama Python library for Ollama access

Prefer asynchronous I/O for API calls.

Use typed Python code.

Validate all external input.

Avoid global mutable state.

Do not put model-specific business logic in route handlers.

---

## 12. AI Provider Interface

Define a stable interface similar to:

```python
from typing import AsyncIterator, Protocol

class AIProvider(Protocol):
    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        stream: bool = True,
        **options,
    ) -> AsyncIterator[str]:
        ...
```

The exact interface may evolve, but application code should depend on the interface rather than an Ollama-specific implementation.

The router should select a provider/node.

---

## 13. Node Health

Each node needs a health state.

Recommended states:

```text
healthy
degraded
offline
disabled
```

Health checks should verify:

1. network connectivity
2. Ollama API responsiveness
3. configured model availability

If a node is offline:

- do not continuously retry in a tight loop
- mark it degraded/offline
- return a useful error or route to another enabled node
- log the event

---

## 14. Failure Behavior

If the requested node fails:

```text
request
  |
  +--> selected node
          |
          +--> success -> return response
          |
          +--> failure -> fallback node if allowed
```

Fallback must be configurable.

Do not silently switch models for tasks where model identity affects testing.

For benchmarking, preserve:

```text
requested_model
actual_model
node_id
latency_ms
```

in the request record.

---

## 15. Conversation Data Model

At minimum:

### users

```text
id
username
display_name
password_hash / external_identity
is_active
created_at
```

### conversations

```text
id
user_id
title
created_at
updated_at
```

### messages

```text
id
conversation_id
role
content
model
node_id
created_at
latency_ms
```

Optional later:

```text
prompt_tokens
completion_tokens
total_tokens
finish_reason
```

Do not store passwords in plain text.

---

## 16. Logging

Log enough information to diagnose the test system, but do not log sensitive message content by default.

Recommended fields:

```text
timestamp
request_id
user_id
conversation_id
node_id
model
latency_ms
status
error_type
```

For development-only debugging, sensitive-content logging must be explicitly enabled.

---

## 17. Model Benchmarking

Create a simple benchmark script.

The benchmark should test both models against the same prompts.

Use categories such as:

```text
1. summarization
2. email drafting
3. rewriting
4. structured JSON
5. basic reasoning
6. spreadsheet formula explanation
7. document extraction
8. short business report
```

Record:

```text
model
node
prompt_id
latency_ms
output_length
success/failure
human_quality_score
```

Human evaluation is required.

Do not select the "best" model based only on tokens/second.

A model that is slightly slower but produces much better office documents may be the better choice.

---

## 18. Laptop Resource Policy

Each 12 GB laptop is a constrained test node.

Default policy:

```text
1 active generation
1 model loaded
moderate context
moderate output length
no background model switching
no simultaneous large document indexing
```

If the laptop becomes unstable:

1. reduce context size
2. reduce output limit
3. disable concurrency
4. test CPU mode
5. move heavy application/database tasks off the laptop

Do not solve memory pressure by adding uncontrolled concurrency.

---

## 19. Application vs Inference Responsibilities

### Python application owns

- authentication
- authorization
- conversations
- users
- file permissions
- database access
- tool validation
- business rules
- audit logs
- model routing
- request limits

### Ollama owns

- model loading
- prompt execution
- token generation
- inference

The model should not own business permissions.

---

## 20. Future Scaling Contract

The Phase 1 implementation must make this future architecture possible without rewriting chat logic:

```text
                    AI Router
                       |
          +------------+------------+
          |            |            |
       Ollama 1     Ollama 2     Ollama 3
       GPU server   GPU server   GPU server
```

A future node should be added by configuration, for example:

```env
OLLAMA_NODE_3_URL=http://10.0.0.23:11434
OLLAMA_NODE_3_MODEL=qwen3.5:9b
OLLAMA_NODE_3_ENABLED=true
```

Do not build the router around assumptions that there are exactly two nodes.

---

## 21. Testing Requirements

Every Phase 1 change should preserve:

### Unit tests

- provider behavior
- router selection
- configuration validation
- authentication
- database repositories

### Integration tests

- API -> Ollama
- API -> database
- node unavailable
- fallback behavior

### Manual acceptance tests

1. log in
2. start conversation
3. send prompt to qwen 3 1.7B
4. send same prompt to Qwen 3.5 0.8B
5. receive response
6. verify message persistence
7. restart an Ollama node
8. verify useful failure/fallback behavior
9. reconnect node
10. verify health becomes healthy

---

## 22. Developer Agent Instructions

When modifying this repository:

1. Read this `AGENTS.md` before making changes.
2. Preserve the Phase 1 scope unless the user explicitly changes it.
3. Prefer simple, testable modules.
4. Do not introduce a new infrastructure dependency without a reason.
5. Never hard-code model names, IPs, ports, credentials, or secrets.
6. Keep model-specific code inside the AI provider layer.
7. Never give the model unrestricted database access.
8. Never expose Ollama directly to end users.
9. Add or update tests with meaningful code changes.
10. Keep APIs backward compatible when practical.
11. Report assumptions and limitations in code comments or documentation.
12. Do not silently change the benchmark configuration.
13. Do not replace one model with another without recording the change.
14. Prefer configuration over code changes for deployment differences.

---

## 23. Coding Standards

Use:

- clear names
- type hints
- small functions
- explicit error handling
- structured logging
- dependency injection where appropriate
- environment-driven configuration

Avoid:

- giant route handlers
- hidden network calls
- hard-coded credentials
- hard-coded laptop addresses
- hidden retries
- silent fallback between benchmark models
- arbitrary SQL generated by the LLM

---

## 24. Definition of Done for Phase 1

Phase 1 is complete when:

- two laptops can independently run their assigned Ollama model
- the Python application can reach both nodes
- a user can select or route to either model
- chat responses are stored
- users can see conversation history
- node health is visible
- a node outage produces a controlled result
- model/node identity is captured for testing
- basic authentication works
- no secrets are committed
- automated tests pass
- the same application can later support more Ollama nodes through configuration

---

## 25. Initial Commands

Install and verify models on Laptop 1:

```bash
ollama pull qwen3:1.7b
ollama list
ollama run qwen3:1.7b
```

Install and verify models on Laptop 2:

```bash
ollama pull qwen3.5:0.8b
ollama list
ollama run qwen3.5:0.8b
```

Verify the Ollama API locally:

```bash
curl http://localhost:11434/api/tags
```

Then configure the Python application with the LAN addresses of the two laptops.

Do not commit the actual `.env` file.

---

## 26. First Implementation Order

Implement in this order:

```text
1. project skeleton
2. configuration system
3. database/session layer
4. user/authentication layer
5. AIProvider interface
6. OllamaProvider
7. two-node router
8. node health checks
9. chat API
10. conversation persistence
11. basic web UI
12. benchmark script
13. tests
14. local deployment documentation
```

Do not start document generation, RAG, or company database integration until this Phase 1 foundation is stable.

---

## 27. Success Metric

The goal of this phase is not maximum intelligence.

The goal is to prove:

> Two low-resource laptops can provide a reliable local AI service through one Python application, while keeping inference nodes replaceable and the application ready for future scale-out.

Once this works, hardware can be upgraded independently of the application.

---

## 28. Current Reference Links

Ollama qwen 3 library:
https://ollama.com/library/qwen3

Ollama Qwen 3.5 library:
https://ollama.com/library/qwen3.5
