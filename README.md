# Casino Game Service (Python)

Worker service that drives external game backends for the Arcadia Laravel app. It receives
signed Arcadia REST triggers (`/create`, `/recharge`, `/withdraw`, `/reset-password`, `/freeplay`,
`/read`), acks `202`, runs the operation against the game backend on an `arq`/Redis worker, and
reports the result via a signed webhook. **Money/account tables are owned by Arcadia** — this
service reads the shared MySQL and never writes money state.

Wire contract: `../../laravel/arcadia/docs/AUTOMATION_SERVICE_CONTRACT.md`.

## Quick start (local, Docker)

```bash
cp .env.example .env        # set API_SECRET + WEBHOOK_SECRET to match Arcadia, APP_URL, DB_*, REDIS_URL
make up                     # api (:8001) + worker + redis
```

The outbound webhook is sent to `{APP_URL}/api/automation/webhook`.

## Quick start (local, no Docker)

```bash
make install                # pip install -e ".[dev]"
make test                   # run the test suite
uvicorn app.main:app --reload --port 8001   # needs a reachable Redis + MySQL
arq app.worker.settings.WorkerSettings      # in a second shell
```

## Layout

See `docs/architecture.md`. Design and plan: `docs/superpowers/specs/` and `docs/superpowers/plans/`.
