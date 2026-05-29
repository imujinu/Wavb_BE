# RAG 검색 파이프라인 구현 플랜

## Context

오디오 업로드 → STT → 청킹 → 임베딩 저장까지의 ingestion 파이프라인은 완성되어 있다.
`search_chunks` 테이블에 벡터 임베딩이 저장되어 있고, `chunks` 테이블에 부모 맥락 청크가 있다.
이제 저장된 데이터를 실제로 검색하고 LLM으로 답변을 생성하는 retrieval + generation 파이프라인이 필요하다.

세 가지 검색 모드를 순차적으로 구현한다:
- **키워드 검색**: PostgreSQL FTS (`to_tsvector` / `plainto_tsquery`) — 정확한 단어 일치에 강함
- **맥락 유사도 검색**: pgvector cosine similarity (`<=>`) — 의미 기반 유사도에 강함
- **하이브리드 검색**: 키워드 + 벡터 결과를 RRF(Reciprocal Rank Fusion)로 병합 — 두 방식의 장점 결합

---

## 서비스 동작 흐름 (완성 후)

```
POST /rag/query
  { query, search_mode, transcript_id?, top_k? }
  ↓
RagQueryService.search(query, search_mode, ...)
  ├── (vector/hybrid) EmbeddingService.embed([query]) → query_embedding
  ├── (keyword)  RagRepository.search_chunks_by_keyword(query, ...) → list[SearchChunkHit]
  ├── (vector)   RagRepository.search_chunks_by_vector(embedding, ...) → list[SearchChunkHit]
  └── (hybrid)   RagRepository.search_chunks_hybrid(query, embedding, ...) → list[SearchChunkHit]
  ↓
RagRepository.get_parent_chunks([hit.parent_chunk_id, ...]) → list[ParentChunkResult]
  ↓
RagResponseService.generate(query, parent_chunks) → answer: str, sources: list
  ↓
200 RagQueryResponse { answer, sources, search_mode, chunks_retrieved }
```

---

## Step별 구현 계획

### Step 1: 검색 스키마 추가
**작업:** `schemas/rag.py`에 검색 관련 모델 추가

**추가 모델:**
```python
SearchMode = Literal["keyword", "vector", "hybrid"]

class SearchChunkHit(BaseModel):        # 검색 결과 단위 (search_chunks 행 + 점수)
    id: UUID
    transcript_id: UUID
    parent_chunk_id: UUID
    child_index: int
    start_seconds: float | None
    end_seconds: float | None
    text: str
    score: float                        # keyword: ts_rank, vector: 1-cosine_dist, hybrid: RRF
    embedding_model: str | None = None

class ParentChunkResult(BaseModel):     # parent hydration 후 맥락 청크 전체 정보
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
    query: str = Field(min_length=1)
    search_mode: SearchMode = "hybrid"
    transcript_id: UUID | None = None   # None이면 전체 transcript 대상
    user_id: UUID | None = None         # JWT 도입 전 임시 — 추후 토큰으로 대체
    top_k: int = Field(default=5, ge=1, le=20)

class RagQueryResponse(BaseModel):
    answer: str
    sources: list[ParentChunkResult]
    search_mode: SearchMode
    chunks_retrieved: int
```

**필요성:** 검색 결과와 쿼리 인터페이스의 타입 정의 없이는 서비스/라우터 구현 불가

---

### Step 2: FTS 인덱스 마이그레이션
**작업:** `server/db/migrations/003_add_search_chunks_fts.sql` 신규 생성

```sql
CREATE INDEX IF NOT EXISTS idx_search_chunks_text_fts
  ON search_chunks USING GIN (to_tsvector('simple', text));
```

**설정 이유:**
- `'simple'` dictionary 사용 이유: 한국어는 영어의 stemming 규칙이 적용되지 않으므로 형태소 분석 없이 원문 토큰을 그대로 인덱싱하는 `simple`이 적합하다. `korean` dictionary는 별도 extension 필요
- GIN 인덱스 이유: 전문 검색에서 역인덱스 구조인 GIN이 B-tree 대비 빠르다

**필요성:** GIN 인덱스 없이는 FTS 쿼리가 full table scan으로 동작하므로 대용량에서 느리다

---

### Step 3: 검색 Repository 메서드 추가
**작업:** `repositories/rag_repository.py`에 4개 메서드 추가

**추가 메서드 시그니처:**
```python
async def search_chunks_by_keyword(
    self, query: str, transcript_id: UUID | None, user_id: UUID | None,
    domain_type: str | None, top_k: int,
) -> list[SearchChunkHit]:
    # SQL: sc JOIN transcripts t ON sc.transcript_id = t.id
    #      WHERE to_tsvector('simple', sc.text) @@ plainto_tsquery('simple', $1)
    #      ORDER BY ts_rank(to_tsvector('simple', sc.text), plainto_tsquery('simple', $1)) DESC

async def search_chunks_by_vector(
    self, embedding: list[float], transcript_id: UUID | None, user_id: UUID | None,
    domain_type: str | None, top_k: int,
) -> list[SearchChunkHit]:
    # SQL: sc.embedding <=> $1::vector AS distance
    #      score = 1.0 - distance (cosine similarity)

async def search_chunks_hybrid(
    self, query: str, embedding: list[float], transcript_id: UUID | None,
    user_id: UUID | None, domain_type: str | None, top_k: int,
) -> list[SearchChunkHit]:
    # asyncio.gather(keyword, vector) → RRF 병합 (k=60)
    # rrf_score = 1/(k + rank_keyword) + 1/(k + rank_vector)
    # model_copy(update={"score": rrf_score}) for frozen SearchChunkHit

async def get_parent_chunks(
    self, parent_chunk_ids: list[UUID],
) -> list[ParentChunkResult]:
    # WHERE id = ANY($1::uuid[]) → chunks 테이블 조회
```

**RRF 설정 이유:**
- `k=60`: BM25와 vector 스코어의 스케일 차이를 무시하고 순위만 사용하는 rank fusion. k=60은 문헌에서 범용적으로 검증된 값으로, 초기 순위를 지나치게 강조하지 않으면서 상위 결과에 적절한 가중치를 줌

**필요성:** 검색 쿼리 없이는 RAG 파이프라인의 R(Retrieval)이 불가

---

### Step 4: RAG 쿼리 서비스
**작업:** `services/rag_query_service.py` 신규 생성

**역할:** 검색 모드에 따라 올바른 Repository 메서드를 선택하고 parent hydration까지 조율

```python
class RagQueryService:
    def __init__(self, repository: RagRepository, embedding_service: EmbeddingService) -> None:

    async def search(
        self,
        query: str,
        search_mode: SearchMode,
        transcript_id: UUID | None,
        user_id: UUID | None,
        domain_type: str | None,
        top_k: int,
    ) -> list[ParentChunkResult]:
        # 1. vector/hybrid면 query embedding 생성
        # 2. search_mode에 따라 Repository 메서드 호출
        # 3. hits에서 unique parent_chunk_ids 추출
        # 4. get_parent_chunks()로 hydration
        # 5. hit 점수 기준 정렬된 ParentChunkResult 반환
```

**EmbeddingService 재사용:** `services/embedding_service.py`에 이미 `embed(texts: list[str])` 구현되어 있음 — 쿼리 1개를 `embed([query])` 형태로 호출

**필요성:** 검색 모드 분기 + embedding 생성 + parent hydration을 라우터에 두면 단일 책임 원칙 위반

---

### Step 5: RAG 응답 생성 서비스
**작업:** `services/rag_response_service.py` 신규 생성

**역할:** 검색된 parent chunks를 context로 GPT에 전달하여 자연어 답변 생성

```python
class RagResponseService:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=...)
        self._model = settings.openai_summary_model  # gpt-4o-mini

    async def generate(
        self,
        query: str,
        parent_chunks: list[ParentChunkResult],
    ) -> str:
        # 1. parent_chunks → context 문자열 구성
        #    "[청크 1] topic: X\n{text}\n\n[청크 2] ..."
        # 2. GPT chat completion 호출
        #    시스템 프롬프트: 한국어 회의/강의 녹취 기반 Q&A 전문가
        # 3. 답변 텍스트 반환
```

**프롬프트 설계 기준:**
- 검색된 청크 범위 내에서만 답변하도록 지시 (hallucination 방지)
- topic/subtopic/keywords를 context에 포함해 LLM이 도메인을 파악하도록 보조

**설정 이유:**
- `gpt-4o-mini` 재사용: 기존 요약 서비스와 동일 모델 → 새 설정 불필요, 비용 절감
- context 길이: parent_chunks 최대 5개 × 평균 500자 ≈ 2500자 — gpt-4o-mini 처리 범위 내

**필요성:** 검색 결과를 그대로 반환하는 것은 RAG가 아닌 단순 검색. 생성(G) 단계가 있어야 질의응답 UX

---

### Step 6: API 엔드포인트 + 라우터 등록
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
    parent_chunks = await rag_query_service.search(...)
    answer = await rag_response_service.generate(request.query, parent_chunks)
    return RagQueryResponse(
        answer=answer,
        sources=parent_chunks,
        search_mode=request.search_mode,
        chunks_retrieved=len(parent_chunks),
    )
```

**수정 파일: `server/main.py`**
- `from routes.rag import router as rag_router` 추가
- `app.include_router(rag_router)` 추가

**필요성:** 엔드포인트 없이는 외부에서 RAG 파이프라인을 호출할 방법이 없음

---

## 파일 목록 요약

### 신규 생성 (4개)
| 파일 | 역할 |
|------|------|
| `server/db/migrations/003_add_search_chunks_fts.sql` | search_chunks FTS GIN 인덱스 |
| `server/services/rag_query_service.py` | 검색 모드 분기 + parent hydration |
| `server/services/rag_response_service.py` | LLM 기반 답변 생성 |
| `server/routes/rag.py` | POST /rag/query 엔드포인트 |

### 수정 (3개)
| 파일 | 변경 내용 |
|------|-----------|
| `server/schemas/rag.py` | SearchChunkHit, ParentChunkResult, RagQueryRequest/Response 추가 |
| `server/repositories/rag_repository.py` | keyword/vector/hybrid search + parent hydration 메서드 4개 추가 |
| `server/main.py` | rag_router include 추가 |

---

## 검증 방법

```bash
cd server

# 관련 테스트만 실행
uv run pytest tests/test_rag_search_repository.py tests/test_rag_query_service.py -v

# 서버 실행 후 직접 호출 테스트
uv run uvicorn main:app --reload

# 키워드 검색
curl -X POST http://localhost:8000/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "다음 출시 일정", "search_mode": "keyword", "top_k": 3}'

# 맥락 유사도 검색
curl -X POST http://localhost:8000/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "역전파 알고리즘", "search_mode": "vector", "top_k": 3}'

# 하이브리드 검색
curl -X POST http://localhost:8000/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "팀 미팅 결정 사항", "search_mode": "hybrid", "top_k": 5}'
```

---

## 최종 산출물

구현 완료 후 `server/plan/rag-search-pipeline.md` 파일 생성 (PLAN.md 규칙 8)
