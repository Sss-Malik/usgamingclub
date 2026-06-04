# Casino Game Service (Python)

Worker service that drives external game backends for the Laravel `casino-app`. It receives a
signed `POST /operations` trigger, acks `202`, runs the operation against the game backend on an
`arq`/Redis worker, and reports the result via a signed webhook. **Money/account tables are owned by
Laravel** — this service reads the shared MySQL and never writes money state.

Wire contract: `../laravel/casino-app/docs/integrations/python-game-service-api-contract.md`.

## Quick start (local, Docker)

```bash
cp .env.example .env        # set PYTHON_SIGNING_SECRET to match Laravel, APP_URL, DB_*, REDIS_URL
make up                     # api (:8001) + worker + redis
make ping                   # verify HMAC end-to-end against Laravel /webhooks/_ping -> 200
```

## Quick start (local, no Docker)

```bash
make install                # pip install -e ".[dev]"
make test                   # run the test suite
uvicorn app.main:app --reload --port 8001   # needs a reachable Redis + MySQL
arq app.worker.settings.WorkerSettings      # in a second shell
```

## Layout

See `docs/architecture.md`. Phase-1 scope and design: `docs/superpowers/specs/`.
