# RAG 검색 파이프라인 구현 플랜

## Context

오디오 업로드 → STT → 청킹 → 임베딩 저장까지의 ingestion 파이프라인은 완성되어 있다.
`search_chunks` 테이블에 벡터 임베딩이 저장되어 있고, `chunks` 테이블에 부모 맥락 청크가 있다.
이제 저장된 데이터를 실제로 검색하고 LLM으로 답변을 생성하는 retrieval + generation 파이프라인이 필요하다.

검색 방식은 **하이브리드 단일 전략**으로 고정한다:
- 내부적으로 키워드(FTS) + 맥락 유사도(pgvector)를 각각 실행한 뒤 **RRF(Reciprocal Rank Fusion)**로 통합
- 원점수를 직접 더하지 않는 이유: `ts_rank`(0.0x대)와 cosine similarity(0.7~0.9대)는 스케일이 달라 가중 합산 시 벡터 점수가 항상 지배 → 키워드 가중치가 무의미해짐
- RRF는 원점수 대신 **각 채널의 순위(rank)**만 사용하므로 스케일 불일치를 원천 제거
- **키워드 가중치 0.6 / 유사도 가중치 0.4** — 순위 기여 비율로 적용, 정확한 단어 매칭을 우선하되 의미 유사성을 보완

한국어 FTS 정확도를 위해 **MeCab 형태소 분석기**를 사용한다:
- 청크 저장 시점에 형태소 분석 → `text_morphemes` 컬럼에 저장 → GIN 인덱스
- 쿼리 시점에 형태소 분석 → FTS 쿼리로 변환
- "일정을", "일정이" 같은 조사 결합 형태도 "일정" 쿼리에 매칭 가능
---

## 서비스 동작 흐름 (완성 후)

### 저장 파이프라인 (수정)
```
search_chunks 생성 (SearchChunkBuilder)
  └── MorphemeService.tokenize(text) → text_morphemes  ← 추가
      예: "일정을 논의했다" → "일정 논의"
  ↓
insert_search_chunks(text_morphemes 포함)
```

### 검색 파이프라인 (신규)
```
POST /rag/query
  { query, transcript_id?, top_k? }
  ↓
RagQueryService.search(query, ...)
  ├── MorphemeService.tokenize(query) → morpheme_query
  │     예: "일정 논의했던 내용" → "일정 논의 내용"
  ├── EmbeddingService.embed([query]) → query_embedding  ← 원문 그대로 임베딩
  ├── RagRepository._search_by_keyword(morpheme_query, ...)
  │     FTS: to_tsvector('simple', text_morphemes) @@ plainto_tsquery('simple', morpheme_query)
  ├── RagRepository._search_by_vector(query_embedding, ...)
  │     pgvector: embedding <=> $1::vector
  └── RRF 융합: score = Σ weight_i / (rrf_k + rank_i)
        = 0.6 / (60 + keyword_rank) + 0.4 / (60 + vector_rank)
  ↓
RagRepository.get_parent_chunks([hit.parent_chunk_id, ...]) → list[ParentChunkResult]
  ↓
RagResponseService.generate(query, parent_chunks) → answer: str
  ↓
200 RagQueryResponse { answer, sources, chunks_retrieved }
```

---

## Step별 구현 계획

### Step 1: 검색 스키마 추가
**작업:** `schemas/rag.py`에 검색 관련 모델 추가

**추가 모델:**
```python
class SearchChunkHit(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    transcript_id: UUID
    parent_chunk_id: UUID
    child_index: int
    start_seconds: float | None
    end_seconds: float | None
    text: str
    score: float                    # 0.6 * keyword_score + 0.4 * vector_score
    embedding_model: str | None = None

class ParentChunkResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    transcript_id: UUID
    domain_type: str
    chunk_index: int
    topic: str | None
    subtopic: str | None
    keywords: list[str]
    speaker_labels: list[str]
    start_seconds: float | None
    end_seconds: float | None
    text: str
    summary: str | None
    metadata: dict[str, Any]

class RagQueryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    query: str = Field(min_length=1)
    transcript_id: UUID | None = None
    user_id: UUID | None = None     # JWT 도입 전 임시 — 추후 토큰으로 대체
    top_k: int = Field(default=5, ge=1, le=20)

class RagQueryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    answer: str
    sources: list[ParentChunkResult]
    chunks_retrieved: int
```

**`SearchChunkCreate` 스키마 수정:**
```python
class SearchChunkCreate(BaseModel):
    ...
    text_morphemes: str | None = None   # MorphemeService가 생성, None이면 text 원문으로 fallback
```

**필요성:** 검색 결과 타입 정의 없이는 서비스/라우터 구현 불가. `text_morphemes` 필드 추가는 ingestion 파이프라인과 insert SQL을 함께 수정하기 위해 이 Step에서 정의

---

### Step 2: MorphemeService 추가
**작업:** `services/morpheme_service.py` 신규 생성, `pyproject.toml` 의존성 추가

**신규 파일: `server/services/morpheme_service.py`**
```python
from kiwipiepy import Kiwi
from kiwipiepy.utils import Stopwords

# 검색에 유효한 품사 태그
_CONTENT_TAGS = {
    "NNG",  # 일반명사
    "NNP",  # 고유명사
    "NNB",  # 의존명사
    "VV",   # 동사
    "VA",   # 형용사
    "MAG",  # 일반부사
    "SL",   # 외래어
    "SH",   # 한자
}

class MorphemeService:
    def __init__(self) -> None:
        self._kiwi = Kiwi()

    def tokenize(self, text: str) -> str:
        # kiwi.tokenize() → Token 목록에서 _CONTENT_TAGS 품사만 필터링 → 공백 구분 문자열
        # 예: "다음 출시 일정을 논의했다" → "다음 출시 일정 논의"
        tokens = self._kiwi.tokenize(text)
        morphemes = [t.form for t in tokens if t.tag in _CONTENT_TAGS]
        return " ".join(morphemes) if morphemes else text
```

**`pyproject.toml` 추가:**
```
"kiwipiepy>=0.18.0"
```

**kiwipiepy 선택 이유:**
- 순수 Python 패키지 (`pip install kiwipiepy` 만으로 설치 완료, 시스템 의존성 없음)
- Windows/macOS/Linux 모두 동일하게 동작 — MeCab처럼 플랫폼별 바이너리 설치 불필요
- 내장 한국어 사전으로 명사·동사·부사 품사 태그 제공
- `t.tag`로 품사 필터링이 직관적이고, `t.form`이 어간 형태를 반환

**`tokenize()` fallback 이유:** 형태소 추출 결과가 빈 목록이면 원문 그대로 반환해 검색 누락 방지

**필요성:** MorphemeService 없이는 "일정을" → "일정" 변환 불가 → 키워드 검색 정확도 저하

---

### Step 3: DB 마이그레이션 — text_morphemes 컬럼 + FTS 인덱스
**작업:** `server/db/migrations/003_add_search_chunks_morphemes.sql` 신규 생성

```sql
ALTER TABLE search_chunks
  ADD COLUMN IF NOT EXISTS text_morphemes TEXT;

CREATE INDEX IF NOT EXISTS idx_search_chunks_morphemes_fts
  ON search_chunks
  USING GIN (to_tsvector('simple', coalesce(text_morphemes, text)));
```

**설계 이유:**
- `text_morphemes` 별도 컬럼: 원본 텍스트(`text`)를 보존하면서 FTS 전용 형태소 텍스트를 분리 관리
- `coalesce(text_morphemes, text)`: `text_morphemes`가 NULL인 기존 rows도 FTS 인덱스에 포함되도록 fallback
- `'simple'` dictionary: 형태소 분석으로 이미 어간이 분리된 상태이므로 PostgreSQL 추가 stemming 불필요

**필요성:** 인덱스 없이는 FTS 쿼리가 full table scan으로 동작

---

### Step 4: 인제스션 파이프라인에 형태소 추출 연결
**작업:** `services/search_chunk_builder.py` 수정, `repositories/rag_repository.py` 수정

**수정: `SearchChunkBuilder`**
```python
class SearchChunkBuilder:
    def __init__(self, morpheme_service: MorphemeService | None = None) -> None:
        self._morpheme_service = morpheme_service

    def build(...) -> list[SearchChunkCreate]:
        # 기존 로직 유지
        # MorphemeService가 주입된 경우: text_morphemes = morpheme_service.tokenize(chunk.text)
        # 없는 경우: text_morphemes = None (FTS에서 원문 fallback)
```

**수정: `insert_search_chunks()` SQL**
```sql
INSERT INTO search_chunks (
  id, transcript_id, parent_chunk_id, child_index,
  segment_start_index, segment_end_index, start_seconds, end_seconds,
  text, text_morphemes, embedding_model, embedding, metadata
)
VALUES ($1, ..., $10, $11, ...)  -- text_morphemes 파라미터 추가
```

**수정: `TranscriptIngestionService`**
- `MorphemeService()` 인스턴스 생성 후 `SearchChunkBuilder(morpheme_service=...)` 에 주입

**필요성:** 인덱스 시점에 `text_morphemes`가 채워지지 않으면 이후 FTS 검색이 동작하지 않는다

---

### Step 5: 검색 Repository 메서드 추가
**작업:** `repositories/rag_repository.py`에 4개 메서드 추가

```python
async def search_chunks_hybrid(
    self,
    morpheme_query: str,        # MeCab 형태소 분석 결과
    embedding: list[float],
    transcript_id: UUID | None,
    user_id: UUID | None,
    top_k: int,
    keyword_weight: float = 0.6,
    vector_weight: float = 0.4,
    rrf_k: int = 60,            # RRF 완충 상수 (문서 수와 무관, 관례값)
) -> list[SearchChunkHit]:
    # asyncio.gather(_search_by_keyword, _search_by_vector) 병렬 실행
    # 각 채널 결과는 자체 점수순 정렬 → 리스트 인덱스 + 1 = 순위(rank)
    # RRF 병합: score(doc) = Σ weight_i / (rrf_k + rank_i)
    #   - 한쪽 채널에만 hit된 경우 해당 채널 기여분만 합산
    #   - 원점수(ts_rank / cosine)는 순위 산출에만 쓰고 합산에는 미사용

async def _search_by_keyword(
    self, morpheme_query: str, transcript_id: UUID | None, user_id: UUID | None, top_k: int,
) -> list[SearchChunkHit]:
    # WHERE to_tsvector('simple', coalesce(text_morphemes, text))
    #         @@ plainto_tsquery('simple', $1)
    # score = ts_rank(to_tsvector(...), plainto_tsquery(...))

async def _search_by_vector(
    self, embedding: list[float], transcript_id: UUID | None, user_id: UUID | None, top_k: int,
) -> list[SearchChunkHit]:
    # sc.embedding <=> $1::vector AS distance
    # score = 1.0 - distance (cosine similarity)

async def get_parent_chunks(
    self, parent_chunk_ids: list[UUID],
) -> list[ParentChunkResult]:
    # WHERE id = ANY($1::uuid[]) → chunks 조회
    # 빈 목록 early return
```

**가중치 이유:**
- `keyword_weight=0.6`: 회의/강의 도메인에서 고유명사·일정·이름은 정확한 단어 일치가 의미 벡터보다 신뢰도 높음
- `vector_weight=0.4`: "일정" 쿼리에 "스케줄", "출시 날짜" 같은 의미적 유사 표현을 보완
- RRF에서 weight는 각 채널의 **순위 기여 비율**로 작동 — 원점수 스케일과 무관하게 0.6 : 0.4 비율이 보존됨

**`rrf_k` 이유:**
- 분모 `1 / (rrf_k + rank)`의 완충 상수. **문서 수가 아니라** 상위 순위 간 점수 격차를 조절하는 값
- `rrf_k`가 작으면 1등이 독식, 크면 상위권이 평준화됨. 60은 원논문(Cormack et al., 2009) 및 ES 기본 관례값
- 코퍼스 크기와 무관하므로 문서가 늘어도 수정 불필요. 변별력 튜닝이 필요할 때만 검증셋으로 조정

**필요성:** 검색 메서드 없이는 retrieval 불가

---

### Step 6: RAG 쿼리 서비스
**작업:** `services/rag_query_service.py` 신규 생성

```python
class RagQueryService:
    def __init__(
        self,
        repository: RagRepository,
        embedding_service: EmbeddingService,
        morpheme_service: MorphemeService,
    ) -> None:

    async def search(
        self,
        query: str,
        transcript_id: UUID | None,
        user_id: UUID | None,
        top_k: int,
    ) -> list[ParentChunkResult]:
        # 1. morpheme_service.tokenize(query) → morpheme_query
        # 2. embedding_service.embed([query]) → [query_embedding]  (원문으로 임베딩)
        # 3. repository.search_chunks_hybrid(morpheme_query, query_embedding, ...)
        # 4. unique parent_chunk_ids 추출 (score 기준 정렬 유지)
        # 5. repository.get_parent_chunks(parent_chunk_ids) → list[ParentChunkResult]
```

**임베딩에 원문 사용 이유:** 형태소 분석 결과("일정 논의")보다 원문("다음 일정 논의했던 내용")이 의미 벡터 공간에서 더 정확한 위치를 갖는다. FTS는 morpheme_query를 사용하고 벡터는 원문을 사용해 각 방식의 장점을 최대화한다

**필요성:** embedding + morpheme 처리 + search + hydration 조율을 라우터에 두면 단일 책임 원칙 위반

---

### Step 7: RAG 응답 생성 서비스
**작업:** `services/rag_response_service.py` 신규 생성

```python
class RagResponseService:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_summary_model  # gpt-4o-mini

    async def generate(self, query: str, parent_chunks: list[ParentChunkResult]) -> str:
        # context 구성: "[청크 1]\ntopic: X | keywords: a, b\n{text}\n\n[청크 2]\n..."
        # GPT chat completion 호출
        # 시스템 프롬프트: 한국어 회의/강의 녹취 기반 Q&A, 제공된 청크 범위 내에서만 답변
```

**설정 이유:**
- `gpt-4o-mini` 재사용: 기존 요약 서비스와 동일 모델 → 새 설정 불필요
- context 길이: parent_chunks 최대 5개 × 평균 500자 ≈ 2500자 — gpt-4o-mini 처리 범위 내
- hallucination 방지 지시: 청크에 없는 내용은 "해당 내용이 없습니다" 명시 유도

**필요성:** 검색 결과를 그대로 반환하는 것은 단순 검색. LLM 생성(G) 단계가 있어야 자연어 Q&A UX

---

### Step 8: API 엔드포인트 + 라우터 등록
**작업:** `routes/rag.py` 신규 생성, `main.py` 수정

**신규 파일: `server/routes/rag.py`**
```python
router = APIRouter(prefix="/rag", tags=["rag"])

@router.post("/query", response_model=RagQueryResponse)
async def rag_query(
    request: RagQueryRequest,
    rag_query_service: RagQueryService = Depends(get_rag_query_service),
    rag_response_service: RagResponseService = Depends(get_rag_response_service),
) -> RagQueryResponse:
    parent_chunks = await rag_query_service.search(
        query=request.query,
        transcript_id=request.transcript_id,
        user_id=request.user_id,
        top_k=request.top_k,
    )
    answer = await rag_response_service.generate(request.query, parent_chunks)
    return RagQueryResponse(
        answer=answer,
        sources=parent_chunks,
        chunks_retrieved=len(parent_chunks),
    )
```

**수정: `server/main.py`**
- `from routes.rag import router as rag_router` 추가
- `app.include_router(rag_router)` 추가

**필요성:** 엔드포인트 없이는 외부에서 RAG 파이프라인을 호출할 방법이 없음

---

## 파일 목록 요약

### 신규 생성 (5개)
| 파일 | 역할 |
|------|------|
| `server/db/migrations/003_add_search_chunks_morphemes.sql` | text_morphemes 컬럼 + GIN FTS 인덱스 |
| `server/services/morpheme_service.py` | MeCab 형태소 분석 |
| `server/services/rag_query_service.py` | query 전처리 + hybrid search + parent hydration |
| `server/services/rag_response_service.py` | LLM 기반 답변 생성 |
| `server/routes/rag.py` | POST /rag/query 엔드포인트 |

### 수정 (5개)
| 파일 | 변경 내용 |
|------|-----------|
| `server/pyproject.toml` | kiwipiepy 추가 |
| `server/schemas/rag.py` | SearchChunkHit, ParentChunkResult, RagQueryRequest/Response 추가, SearchChunkCreate에 text_morphemes 추가 |
| `server/repositories/rag_repository.py` | hybrid/keyword/vector search + parent hydration 메서드 4개 추가, insert_search_chunks SQL에 text_morphemes 추가 |
| `server/services/search_chunk_builder.py` | MorphemeService 주입 + text_morphemes 생성 |
| `server/main.py` | rag_router include 추가 |

(`TranscriptIngestionService`는 SearchChunkBuilder 생성자 호출 변경만 필요)

---

## 검증 방법

```bash
cd server

# 의존성 설치 (MeCab 시스템 설치 후)
uv sync

# 관련 테스트만 실행
uv run pytest tests/test_morpheme_service.py tests/test_rag_search_repository.py tests/test_rag_query_service.py -v

# 서버 실행 후 직접 호출
uv run uvicorn main:app --reload

# 하이브리드 검색 (항상 hybrid)
curl -X POST http://localhost:8000/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "다음 출시 일정 논의했던 내용", "top_k": 5}'

# transcript 범위 지정
curl -X POST http://localhost:8000/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "역전파 기울기", "transcript_id": "<uuid>", "top_k": 3}'
```
