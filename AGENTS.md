# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

## Commands

All commands run from the `server/` directory.

```bash
# Install dependencies
uv sync

# Start development server (http://localhost:8000)
uv run uvicorn main:app --reload

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_specific.py

# Start PostgreSQL (required for DB-dependent features)
docker-compose up -d
```

## Environment Setup

Copy `server/.env.example` to `server/.env` and configure:
- `DATABASE_URL` — asyncpg connection string (default targets `localhost:5432/recordoc`)
- `OPENAI_API_KEY` — required for Whisper STT and GPT summarization
- All other settings have working defaults (see `server/settings.py`)

## Architecture

FastAPI backend for audio transcription and RAG ingestion.
Turns audio files into searchable, enriched text chunks stored in PostgreSQL with pgvector.

### Layers

- **Routes** (`server/routes/`): Two endpoints — `/audio/summarize` (no persistence), `/audio/transcripts` (full ingestion with DB).
- **Services** (`server/services/`): Business logic. Each service is independently instantiable.
- **Repositories** (`server/repositories/`): DB access via `asyncpg`. `RagRepository` handles all persistence.
- **Database** (`server/database/`): `connection.py` manages the async connection pool.
- **Schemas** (`server/schemas/`): Pydantic models. `rag.py` defines `Transcript`, `Segment`, `Chunk`.
- **Settings** (`server/settings.py`): `pydantic-settings`; all config loaded from `.env`.

### Database Schema

PostgreSQL 16 + pgvector. Three core tables:
- `transcripts` — UUID PK, status, full_text, summary
- `segments` — STT units with `start_seconds`, `end_seconds`, `speaker_label`
- `chunks` — `embedding vector(1536)`, `keywords[]`, `topic`, `subtopic`; cosine similarity via ivfflat index

### Key Design Decisions

- **Concurrency**: Whisper semaphore default 3 (`AUDIO_TRANSCRIPTION_CONCURRENCY`), summary semaphore default 2 (`SUMMARY_CONCURRENCY`).
- **Chunk overlap**: `AUDIO_CHUNK_OVERLAP_SECONDS` default 2s; overlap segments deduplicated on merge.
- **LLM fallback**: `ContextChunkPlanningService` falls back to `FallbackChunkBuilder` on failure.
- **Metadata enrichment**: `ChunkMetadataService` is optional — pipeline completes without it.

## External Dependencies

- **FFmpeg** — required at runtime (via `imageio-ffmpeg`)
- **OpenAI API** — Whisper (`whisper-1`) for STT, GPT (`gpt-4o-mini`) for summaries and chunk planning
- **PostgreSQL 16 + pgvector** — vector similarity search on `chunks.embedding`

## Windows 환경 주의사항
- PowerShell로 파일 직접 수정 시 반드시 `-Encoding UTF8` 명시
- `npx` 실행 시 `npx.cmd` 사용 (PS 실행 정책 우회)

## Error Handling Policy

다음 상황에서는 자체 수정 시도 없이 즉시 멈추고 사용자에게 보고할 것:

1. **파일 인코딩 손상** — apply_patch 또는 텍스트 치환 후 구문 오류가 발생한 경우
2. **동일한 오류로 2회 이상 재시도** — 같은 실패가 반복되면 루프 중단
3. **작업 범위 외 파일에서 오류 발생** — 수정하지 않은 파일의 타입/빌드 오류는 건드리지 말고 목록만 보고

보고 형식:
- 발생한 오류 내용
- 시도한 접근법
- 사용자에게 필요한 결정 사항