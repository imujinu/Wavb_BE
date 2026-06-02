# Services 디렉터리 재구성 플랜

**Goal:** `services/` 폴더에 몰려있는 19개 파일을 도메인별 하위 패키지로 이동한다. 로직 변경 없음 — 경로와 import만 변경.

**Architecture:** 파일을 `audio/`, `rag/`, `auth/`, `summary/`, `chunks/` 5개 도메인 패키지로 분류. `realtime/`은 실시간 전사 플랜에서 별도 생성.

---

## 목표 디렉터리 구조

```
server/services/
├── audio/
│   ├── __init__.py
│   ├── audio_analysis_service.py
│   ├── audio_chunking.py
│   ├── transcription_service.py
│   └── transcript_ingestion_service.py
├── rag/
│   ├── __init__.py
│   ├── embedding_service.py
│   ├── morpheme_service.py
│   ├── rag_query_service.py
│   └── rag_response_service.py
├── auth/
│   ├── __init__.py
│   ├── auth_service.py
│   └── oauth_service.py
├── summary/
│   ├── __init__.py
│   ├── pdf_templates.py
│   ├── summary_pdf_service.py
│   ├── summary_service.py
│   └── templated_summary_service.py
└── chunks/
    ├── __init__.py
    ├── chunk_builder.py
    ├── chunk_metadata_service.py
    ├── context_chunk_planning_service.py
    └── search_chunk_builder.py
```

---

## Task 1: 파일 이동 (git mv)

왜 git mv인가: 파일 이동 시 히스토리(blame/log)를 보존합니다.

- [ ] audio 패키지 생성

```bash
cd server
mkdir services/audio
git mv services/audio_analysis_service.py services/audio/
git mv services/audio_chunking.py services/audio/
git mv services/transcription_service.py services/audio/
git mv services/transcript_ingestion_service.py services/audio/
```

- [ ] rag 패키지 생성

```bash
mkdir services/rag
git mv services/embedding_service.py services/rag/
git mv services/morpheme_service.py services/rag/
git mv services/rag_query_service.py services/rag/
git mv services/rag_response_service.py services/rag/
```

- [ ] auth 패키지 생성

```bash
mkdir services/auth
git mv services/auth_service.py services/auth/
git mv services/oauth_service.py services/auth/
```

- [ ] summary 패키지 생성

```bash
mkdir services/summary
git mv services/pdf_templates.py services/summary/
git mv services/summary_pdf_service.py services/summary/
git mv services/summary_service.py services/summary/
git mv services/templated_summary_service.py services/summary/
```

- [ ] chunks 패키지 생성

```bash
mkdir services/chunks
git mv services/chunk_builder.py services/chunks/
git mv services/chunk_metadata_service.py services/chunks/
git mv services/context_chunk_planning_service.py services/chunks/
git mv services/search_chunk_builder.py services/chunks/
```

- [ ] 각 패키지에 `__init__.py` 생성

```bash
# PowerShell
"" | Out-File services/audio/__init__.py -Encoding utf8
"" | Out-File services/rag/__init__.py -Encoding utf8
"" | Out-File services/auth/__init__.py -Encoding utf8
"" | Out-File services/summary/__init__.py -Encoding utf8
"" | Out-File services/chunks/__init__.py -Encoding utf8
```

왜 `__init__.py`인가: Python이 디렉터리를 패키지로 인식하려면 필요합니다.

---

## Task 2: Import 경로 업데이트

### routes/audio.py

```python
# 변경 전
from services.pdf_templates import TemplateSpec, get_template, list_templates
from services.summary_pdf_service import SummaryPdfService
from services.summary_service import SummaryService
from services.templated_summary_service import TemplatedSummaryService
from services.transcript_ingestion_service import TranscriptIngestionService
from services.transcription_service import TranscriptionService

# 변경 후
from services.summary.pdf_templates import TemplateSpec, get_template, list_templates
from services.summary.summary_pdf_service import SummaryPdfService
from services.summary.summary_service import SummaryService
from services.summary.templated_summary_service import TemplatedSummaryService
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.audio.transcription_service import TranscriptionService
```

### routes/auth.py

```python
# 변경 전
from services.auth_service import AuthService
# 변경 후
from services.auth.auth_service import AuthService
```

### routes/oauth.py

```python
# 변경 전
from services.oauth_service import OAuthService
# 변경 후
from services.auth.oauth_service import OAuthService
```

### routes/rag.py

```python
# 변경 전
from services.embedding_service import EmbeddingService
from services.morpheme_service import MorphemeService
from services.rag_query_service import RagQueryService
from services.rag_response_service import RagResponseService

# 변경 후
from services.rag.embedding_service import EmbeddingService
from services.rag.morpheme_service import MorphemeService
from services.rag.rag_query_service import RagQueryService
from services.rag.rag_response_service import RagResponseService
```

### services/rag/rag_query_service.py (내부 cross-service import)

```python
# 변경 전
from services.embedding_service import EmbeddingService
from services.morpheme_service import MorphemeService

# 변경 후
from services.rag.embedding_service import EmbeddingService
from services.rag.morpheme_service import MorphemeService
```

### services/audio/transcription_service.py (내부 cross-service import)

```python
# 변경 전
from services.audio_analysis_service import AudioAnalysisService
from services.audio_chunking import (
    AudioChunk,
    AudioChunkingService,
    build_chunk_plan,
    calculate_chunk_seconds,
)

# 변경 후
from services.audio.audio_analysis_service import AudioAnalysisService
from services.audio.audio_chunking import (
    AudioChunk,
    AudioChunkingService,
    build_chunk_plan,
    calculate_chunk_seconds,
)
```

### services/audio/transcript_ingestion_service.py (가장 많은 변경)

```python
# 변경 전
from services.chunk_builder import (
    ContextPlannedChunkBuilder,
    DeterministicFallbackChunkBuilder,
)
from services.context_chunk_planning_service import ContextChunkPlanningService
from services.chunk_metadata_service import ChunkMetadataService
from services.embedding_service import EmbeddingService
from services.morpheme_service import MorphemeService
from services.search_chunk_builder import SearchChunkBuilder
from services.transcription_service import TranscriptionService, TranscriptionSegment

# 변경 후
from services.chunks.chunk_builder import (
    ContextPlannedChunkBuilder,
    DeterministicFallbackChunkBuilder,
)
from services.chunks.context_chunk_planning_service import ContextChunkPlanningService
from services.chunks.chunk_metadata_service import ChunkMetadataService
from services.rag.embedding_service import EmbeddingService
from services.rag.morpheme_service import MorphemeService
from services.chunks.search_chunk_builder import SearchChunkBuilder
from services.audio.transcription_service import TranscriptionService, TranscriptionSegment
```

### services/summary/templated_summary_service.py

```python
# 변경 전
from services.pdf_templates import TemplateSpec
# 변경 후
from services.summary.pdf_templates import TemplateSpec
```

### services/summary/summary_pdf_service.py

```python
# 변경 전
from services.pdf_templates import TemplateSpec
# 변경 후
from services.summary.pdf_templates import TemplateSpec
```

---

## Task 3: 검증 + 커밋

- [ ] import 체인 검증

```bash
cd server
uv run python -c "
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.rag.rag_query_service import RagQueryService
from services.auth.auth_service import AuthService
from services.summary.summary_service import SummaryService
from services.chunks.chunk_builder import MeetingChunkBuilder
print('모든 import 성공')
"
```

- [ ] 기존 테스트 실행

```bash
uv run pytest tests/ -x --tb=short
```

- [ ] 커밋

```bash
git add -A
git commit -m "refactor: services를 도메인별 하위 패키지로 재구성"
```
