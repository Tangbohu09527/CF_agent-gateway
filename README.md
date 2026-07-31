# CF_agent-gateway

Enterprise AI Message Gateway.

> Status: initialization phase.

CF_agent-gateway is the message and control plane between enterprise message
entry points and Hermes.

```text
Entry points
     |
     v
CF_agent-gateway
     |
     v
Hermes
```

## Responsibilities

- Message Store
- Access Control
- Context Builder
- Task Queue
- AI Router
- AI Provider Registry

The gateway accepts messages from multiple entry points, persists them, applies
access policy, builds execution context, queues work, and routes requests to a
registered AI provider.

## Non-goals

CF_agent-gateway does not provide:

- AI inference
- Skill execution
- ERP business logic
- A WeChat bot
- A Hermes implementation

## Current scope

The initialization phase contains only the service foundation:

- YAML configuration loading
- JSON structured logging
- FastAPI application lifecycle
- SQLAlchemy engine configuration
- `GET /health`
- Container build and Compose service

Message adapters, Hermes integration, AI providers, persistence models,
authorization, context construction, and task processing are intentionally not
implemented.

## Technology baseline

- Python 3.12+
- FastAPI and Uvicorn
- SQLAlchemy 2.x
- SQLite for local and phase-one persistence
- PostgreSQL support through Psycopg 3
- YAML configuration
- Docker deployment

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m cf_agent_gateway.main
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`.

The service reads `config/config.yaml` by default. Set `CF_GATEWAY_CONFIG` to
use a different file.

```bash
curl http://localhost:8080/health
```

Expected response:

```json
{"status":"ok"}
```

## Test

```bash
pytest
ruff check .
```

## Docker

```bash
docker compose up --build
```

See [docs/architecture.md](docs/architecture.md) for module boundaries and the
planned request flow.
