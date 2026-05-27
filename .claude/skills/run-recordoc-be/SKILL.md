---
name: run-recordoc-be
description: run, start, launch, screenshot, smoke test, verify, check the Recordoc FastAPI backend server
---

# run-recordoc-be

FastAPI server (Python + uvicorn). Driven via `smoke.py`, which launches the server as a subprocess, fires httpx probes, and exits 0/1. No DB required for the smoke checks — the probes only hit `/health`, `/openapi.json`, and the file-type validation path (400 without a real audio file or DB connection).

## Prerequisites

- Python 3.11+, `uv` installed
- All commands run from `server/`

```bash
cd server/
uv sync
```

The `httpx` client is already in the `dev` dependency group — no extra install.

## Run (agent path)

```bash
cd server/
uv run python ../.claude/skills/run-recordoc-be/smoke.py
```

The script launches the server on port 8000, runs four checks, prints `[OK]` or `[FAIL]` per check, then shuts down. Exits 1 on first failure.

To use a different port:

```bash
uv run python ../.claude/skills/run-recordoc-be/smoke.py 8001
```

### What the smoke checks verify

| Check | Method | Expected |
|---|---|---|
| `/health` | GET | 200 `{"status":"ok"}` |
| `/openapi.json` | GET | 200, title contains "Recordoc Backend" |
| `/audio/summarize` bad ext | POST `.txt` | 400 |
| `/audio/transcripts` bad ext | POST `.txt` | 400 |

These four checks require no DB and no OpenAI key. They confirm the server starts and routing/validation logic is wired correctly.

## Run (human path)

```bash
cd server/
uv run uvicorn main:app --reload
```

Server listens on `http://localhost:8000`. Swagger UI at `http://localhost:8000/docs`. Ctrl-C to stop. Requires PostgreSQL (via `docker-compose up -d`) and `.env` with `DATABASE_URL` and `OPENAI_API_KEY` for the full pipeline.

## Test suite

```bash
cd server/
uv run pytest
```

51 tests, ~8 s. No DB or real OpenAI calls — all external calls are mocked.

## Gotchas

- **`uv` ignores the active conda env.** If a conda env is active, uv emits a warning but still uses its own `.venv`. The warning is harmless.
- **`smoke.py` path is relative to `server/`.** The script resolves `SERVER_DIR` from its own `__file__`, so it must be run with `uv run python ../.claude/...` from inside `server/`. Running it from the repo root will miscalculate `SERVER_DIR`.
- **curl on Windows bash can't read `/c/...` paths with `@`.** The smoke script uses Python `httpx` with in-memory bytes to avoid curl path conversion issues entirely.
- **`/audio/transcripts` and `/audio/summarize` need a real audio file + DB + OpenAI key to complete.** The smoke checks only exercise the validation layer (400 on bad extension) — no file processing happens.
