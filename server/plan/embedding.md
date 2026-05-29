# Hierarchical Vector Indexing 저장 파이프라인 계획

## Summary

현재 프로젝트는 STT 결과를 `transcripts → segments → chunks`로 RDB에 저장한다. 다음 단계에서는 `chunks`를 **부모 맥락 단위(parent context chunk)** 로 유지하고, 검색용 자식 단위는 parent chunk 내부의 segments를 **adaptive grouping** 해서 만든다.

v1 범위는 **저장 파이프라인만** 구현한다. 검색 API, parent hydrate, rerank는 다음 계획에서 구현한다.

최종 계획 파일은 구현 단계에서 `server/plan/hierarchical-vector-indexing.md`로 생성한다.

## Key Changes

- `chunks`
  - 기존 테이블 유지.
  - 요약자료 생성 및 parent context lookup 용도로 사용한다.
  - 검색용으로 다시 쪼개지 않는다.

- 새 `search_chunks` 테이블 추가
  - vector search 대상이 되는 child search unit 저장소.
  - 각 row는 `parent_chunk_id`로 `chunks.id`를 참조한다.
  - child text는 parent chunk 내부의 segments를 adaptive grouping 해서 만든다.

- 기본 데이터 흐름
```text
transcripts
→ segments
→ chunks(parent context)
→ search_chunks(child vector search unit)
```

## Implementation Plan

### Step 1. DB Schema 추가

필요성:
- `chunks`는 parent context 단위라 검색 단위로는 클 수 있다.
- 검색 정확도를 위해 더 작은 child search unit이 필요하다.

작업:
- 새 migration 추가:
  - `search_chunks`
- 주요 컬럼:
  - `id UUID PRIMARY KEY`
  - `transcript_id UUID REFERENCES transcripts(id) ON DELETE CASCADE`
  - `parent_chunk_id UUID REFERENCES chunks(id) ON DELETE CASCADE`
  - `child_index INT`
  - `segment_start_index INT`
  - `segment_end_index INT`
  - `start_seconds NUMERIC`
  - `end_seconds NUMERIC`
  - `text TEXT NOT NULL`
  - `embedding_model TEXT NOT NULL`
  - `embedding vector(1536)`
  - `metadata JSONB DEFAULT '{}'`
  - `created_at TIMESTAMPTZ DEFAULT now()`
- unique key:
  - `(parent_chunk_id, child_index)`
- indexes:
  - `(transcript_id)`
  - `(parent_chunk_id, child_index)`
  - `ivfflat (embedding vector_cosine_ops)`
  - `GIN(metadata)`

설정 이유:
- 기존 migration이 이미 `pgvector`와 `vector(1536)`을 기준으로 하고 있으므로 같은 기준을 유지한다.
- child table을 분리해 `chunks`의 parent context 역할을 흐리지 않는다.

### Step 2. Adaptive Search Chunk Builder 추가

필요성:
- segment 하나만 embedding하면 의미가 부족할 수 있다.
- 반대로 2~4개를 무조건 묶으면 STT segment 길이에 따라 너무 길어질 수 있다.

작업:
- `SearchChunkCreate` schema 추가.
- `SearchChunkBuilder` 서비스 추가.
- 입력:
  - parent chunks
  - transcript segments
- 출력:
  - parent chunk별 search child chunks
- grouping 기본값:
  - 목표 segment 수: `2~3`
  - 최대 segment 수: `4`
  - 최대 길이: `800 chars`
  - 최대 시간: `90 seconds`
- grouping 규칙:
  - parent chunk의 `segment_start_index ~ segment_end_index` 범위 안에서만 묶는다.
  - 짧은 segment는 앞뒤 segment와 묶는다.
  - 다음 segment를 추가했을 때 최대 길이 또는 최대 시간을 넘으면 현재 child를 닫는다.
  - 긴 segment 하나가 이미 최대 길이에 가깝거나 초과하면 단독 child로 저장한다.
  - parent chunk boundary를 넘지 않는다.
- metadata:
  - `parent_chunk_index`
  - `child_goal: vector_search`
  - `grouping_strategy: adaptive_segments_v1`
  - `segment_count`

결과 예시:
```text
parent chunk 0: segments 0~8

search child 0: segments 0~2
search child 1: segments 3~4
search child 2: segments 5~7
search child 3: segment 8
```

### Step 3. Embedding Service 추가

필요성:
- `search_chunks`를 pgvector로 검색하려면 embedding 생성이 필요하다.

작업:
- `EmbeddingService` 추가.
- OpenAI embeddings API 사용.
- 설정 추가:
  - `OPENAI_EMBEDDING_MODEL`
  - 기본값: `text-embedding-3-small`
- embedding input:
  - v1 기본은 `search_chunk.text`
  - parent topic/summary를 embedding input에 섞지 않는다.
- 실패 처리:
  - embedding 생성 실패 시 transcript/chunks 저장은 실패시키지 않는다.
  - search chunk 저장은 건너뛰고 로그를 남긴다.

설정 이유:
- `text-embedding-3-small`은 기존 `vector(1536)` schema와 맞는다.
- parent metadata를 섞으면 검색 단위가 흐려질 수 있어 v1에서는 child text만 사용한다.

### Step 4. Repository 저장 로직 추가

필요성:
- parent chunks 저장 후 child search chunks를 bulk insert/upsert해야 한다.

작업:
- `RagRepository.fetch_chunks_by_transcript(transcript_id)` 추가.
  - `chunks.id`, `chunk_index`, `segment_start_index`, `segment_end_index`, `start_seconds`, `end_seconds`, `text`, metadata 조회.
- `RagRepository.insert_search_chunks(search_chunks)` 추가.
  - `(parent_chunk_id, child_index)` 기준 upsert.
  - embedding은 기존 `_to_vector_literal()` 방식 재사용.
- 기존 `insert_chunks()`는 동작을 변경하지 않는다.

결과:
```text
chunks 저장
→ parent chunks 조회
→ search_chunks 생성
→ embeddings 생성
→ search_chunks 저장
```

### Step 5. Ingestion 동기 후처리 연결

필요성:
- v1에서는 사용자가 선택한 대로 `/audio/transcripts` 요청 안에서 indexing 저장까지 완료한다.

작업:
- `TranscriptIngestionService` 흐름 확장:
```text
STT
→ transcripts.full_text 저장
→ segments 저장
→ LLM planner 기반 chunks 생성
→ chunk metadata enrich
→ chunks 저장
→ parent chunks 조회
→ adaptive search chunks 생성
→ embeddings 생성
→ search_chunks 저장
→ 응답 반환
```
- embedding/search chunk 단계 실패는 transcript 실패로 처리하지 않는다.
- 실패 시 로그만 남기고 `/audio/transcripts` 응답은 `completed` 유지한다.

영향 최소화:
- 기존 `segments`, `chunks` 저장 흐름은 유지한다.
- 검색 API는 이번 범위에서 추가하지 않는다.

## Test Plan

관련 테스트만 실행한다.

- `tests/test_search_chunk_builder.py`
  - adaptive grouping이 parent range 안에서만 child chunks를 생성하는지 확인
  - 짧은 segments는 2~3개 단위로 묶이는지 확인
  - 긴 segment는 단독 child로 저장되는지 확인
  - 최대 길이/시간을 넘지 않는지 확인
- `tests/test_embedding_service.py`
  - OpenAI embedding 응답을 `list[float]`로 변환하는지 확인
  - provider 실패 시 명확한 예외가 발생하는지 확인
- `tests/test_rag_persistence.py`
  - `search_chunks` migration 생성 확인
  - parent chunk 조회 메서드 확인
  - `insert_search_chunks()` SQL과 vector literal 저장 확인
- `tests/test_transcript_ingestion_service.py`
  - chunks 저장 후 search child chunks 저장까지 호출되는지 확인
  - embedding 실패 시 transcript 저장이 실패하지 않는지 확인

실행 명령:
```bash
uv run pytest tests/test_search_chunk_builder.py tests/test_embedding_service.py tests/test_rag_persistence.py tests/test_transcript_ingestion_service.py
```

## Assumptions

- 벡터 저장소는 별도 외부 Vector DB가 아니라 기존 PostgreSQL `pgvector`를 사용한다.
- `chunks`는 parent context chunk로 유지한다.
- 검색용 child 단위는 새 `search_chunks` 테이블에 저장한다.
- child grouping은 고정 2~4 segments가 아니라 adaptive grouping을 사용한다.
- 기본 grouping 설정은 목표 `2~3 segments`, 최대 `4 segments`, 최대 `800 chars`, 최대 `90 seconds`다.
- embedding model 기본값은 `text-embedding-3-small`이다.
- 이번 범위는 저장 파이프라인까지만이며, child 검색 후 parent hydrate/rerank API는 다음 계획에서 구현한다.
- Plan Mode 종료 후 구현 단계에서 `server/plan/hierarchical-vector-indexing.md` 파일을 생성한다.
