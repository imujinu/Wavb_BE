# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

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

This is a FastAPI backend for audio transcription and RAG (Retrieval-Augmented Generation) ingestion. The core concern is turning audio files into searchable, enriched text chunks stored in PostgreSQL with pgvector.

### Request Flow: `POST /audio/transcripts` (full ingestion pipeline)

```
HTTP Request → Route (audio.py)
    → TranscriptIngestionService.ingest()
        1. Create transcript record (status: "processing")
        2. AudioAnalysisService → FFmpeg → duration
        3. ContextChunkPlanningService → calculate chunk plan (size + overlap)
        4. AudioChunkingService → FFmpeg → split into MP3 chunks
        5. TranscriptionService → OpenAI Whisper (concurrent, semaphore-limited)
        6. Merge segments, resolve overlaps
        7. RagRepository → persist segments
        8. Chunk builders → build retrieval chunks from segments
        9. ChunkMetadataService → LLM enrichment (topics, keywords)
        10. RagRepository → persist chunks
        11. Update transcript status → return result
```

### Layers

- **Routes** (`server/routes/`): FastAPI routers. Only two endpoints: `/audio/summarize` (no persistence) and `/audio/transcripts` (full ingestion with DB).
- **Services** (`server/services/`): Business logic. Each service is independently instantiable with optional dependencies for testability.
- **Repositories** (`server/repositories/`): Database access via `asyncpg`. `RagRepository` handles all persistence for transcripts, segments, and chunks.
- **Database** (`server/database/`): `connection.py` manages the async connection pool.
- **Schemas** (`server/schemas/`): Pydantic models. `rag.py` defines `Transcript`, `Segment`, `Chunk`.
- **Settings** (`server/settings.py`): `pydantic-settings` model; all config loaded from `.env`.

### Database Schema

PostgreSQL 16 + pgvector. Three core tables:
- `transcripts` — top-level record per audio upload (UUID PK, status, full_text, summary)
- `segments` — atomic STT units with timing (`start_seconds`, `end_seconds`, `speaker_label`)
- `chunks` — retrieval-optimized units with `embedding vector(1536)`, `keywords[]`, `topic`, `subtopic`; supports cosine similarity search via ivfflat index

### Key Design Decisions

- **Concurrency**: Whisper calls and summary calls are semaphore-gated (default 3 and 2 respectively). Controlled by `AUDIO_TRANSCRIPTION_CONCURRENCY` and `SUMMARY_CONCURRENCY`.
- **Chunk overlap**: Audio chunks overlap by `AUDIO_CHUNK_OVERLAP_SECONDS` (default 2s) to prevent content loss at boundaries; overlap segments are deduplicated during merge.
- **LLM fallback**: `ContextChunkPlanningService` uses an LLM to plan semantic chunk boundaries; if it fails, a deterministic `FallbackChunkBuilder` takes over.
- **Metadata enrichment**: `ChunkMetadataService` is optional — the pipeline completes without it if the service is unavailable.

## Frontend 프로젝트 참조 (`../Recordoc_FE/`)

프론트엔드는 Expo SDK 54 + React Native + TypeScript로 구성된 모바일 앱입니다.

### 폴더 구조

```
Recordoc_FE/
├── app/                          # expo-router 라우트 (얇은 wrapper)
│   ├── _layout.tsx               # Root Stack: (tabs) + recording modal
│   ├── landing.tsx               # → LandingScreen
│   ├── login.tsx                 # → LoginScreen
│   ├── recording.tsx             # → RecordingScreen
│   ├── detail.tsx                # → DetailScreen
│   └── (tabs)/
│       ├── _layout.tsx           # Tabs + 커스텀 TabBar
│       ├── index.tsx             # → HomeScreen (루트 경로 /)
│       ├── my-work.tsx           # → WorkListScreen
│       ├── chat.tsx              # stub
│       └── more.tsx              # stub
│
├── src/
│   ├── screens/                  # 실제 화면 컴포넌트
│   │   ├── LandingScreen.tsx
│   │   ├── LoginScreen.tsx       # 인증 화면 (POST /auth/*)
│   │   ├── HomeScreen.tsx
│   │   ├── WorkListScreen.tsx    # 업로드된 녹음 목록 (GET /audio/transcripts)
│   │   ├── DetailScreen.tsx      # 트랜스크립트 상세 (GET /audio/transcripts/:id)
│   │   ├── RecordingScreen.tsx   # 녹음 화면 (POST /audio/transcripts - 미연동)
│   │   └── UploadScreen.tsx      # 파일 업로드 (POST /audio/transcripts)
│   │
│   ├── components/
│   │   ├── TabBar.tsx            # 커스텀 탭바
│   │   ├── TabBar.styles.ts
│   │   └── StopRecordingModal.tsx
│   │
│   └── styles/
│       ├── theme.ts              # 공유 색상 토큰
│       ├── HomeScreen.styles.ts
│       └── RecordingScreen.styles.ts
│
├── app.json                      # Expo 앱 설정 (name, bundle ID 등)
├── tsconfig.json                 # @/ → src/ import alias
└── metro.config.js
```

### 화면 ↔ API 매핑

| 화면 | 호출 API |
|------|---------|
| `LoginScreen` | `POST /auth/login`, `POST /auth/register` |
| `WorkListScreen` | `GET /audio/transcripts` |
| `DetailScreen` | `GET /audio/transcripts/:id`, `POST /rag/query` |
| `UploadScreen` | `POST /audio/transcripts` (multipart/form-data) |
| `RecordingScreen` | 미연동 (mock 데이터) — 추후 `POST /audio/transcripts` 또는 실시간 WebSocket |

### 현재 연동 상태

- **미연동**: `RecordingScreen` 타이머·스크립트는 mock 데이터. `StopRecordingModal.onConfirm`은 `router.back()`만 호출.
- **실시간**: `routes/realtime.py` 엔드포인트 존재하나 FE 연동 미완성.

---

## External Dependencies

- **FFmpeg** — required at runtime (provided via `imageio-ffmpeg`)
- **OpenAI API** — Whisper (`whisper-1`) for STT, GPT (`gpt-4o-mini`) for summaries and chunk planning
- **PostgreSQL 16 + pgvector** — vector similarity search on `chunks.embedding`
