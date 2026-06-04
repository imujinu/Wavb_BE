# RAG 문서 검색 API 정리 설계

## Summary

- 이번 단계는 `/rag/query`를 **인증된 사용자 소유 문서 검색 전용 API**로 정리한다.
- 웹 검색과 document+web hybrid 검색은 이번 단계에서 구현하지 않고 `rag_web_hybrid.md`로 분리한다.
- 기존 hybrid search(keyword + vector + RRF)는 유지하되, 검색 범위를 단일 `transcript_id`에서 `transcript_ids: UUID[]`로 확장한다.
- body의 `user_id`는 제거하고 JWT의 `current_user.user_id`만 사용한다.
- 응답은 DB 내부 모델인 `ParentChunkResult`를 그대로 노출하지 않고, 클라이언트용 `RetrievedSource` 형태로 반환한다.

## API Changes

- endpoint는 기존 `POST /rag/query`를 유지한다.
- 요청 모델:

```json
{
  "query": "질문",
  "transcript_ids": ["uuid"],
  "top_k": 5
}
```

- `query`: 검색 질문. 빈 문자열은 422.
- `transcript_ids`: 검색할 transcript UUID 배열. 1개 이상 필수.
- `top_k`: 최종 반환 source 수. 기본값 5, 최대 20.
- `user_id`: 요청 body에서 제거한다.

- 응답 모델:

```json
{
  "answer": "핵심 답변",
  "sources": [
    {
      "source_type": "document",
      "title": "강의 제목 또는 청크 주제",
      "snippet": "핵심 근거 요약 또는 청크 일부",
      "transcript_id": "uuid",
      "url": null,
      "score": 0.87
    }
  ],
  "warnings": []
}
```

## Schema Changes

- `RagQueryRequest`
  - 제거: `user_id`, `transcript_id`
  - 추가: `transcript_ids: list[UUID]`
  - 유지: `query`, `top_k`
- `RetrievedSource`
  - `source_type: Literal["document"]`
  - `title: str`
  - `snippet: str`
  - `transcript_id: UUID | None`
  - `url: str | None`
  - `score: float | None`
  - `metadata: dict[str, Any] = {}`
- `RagQueryResponse`
  - `answer: str`
  - `sources: list[RetrievedSource]`
  - `warnings: list[str] = []`

## Implementation Changes

### 인증

- `routes/rag.py`에 `get_current_user` 의존성을 추가한다.
- `RagQueryService.search()`에는 `current_user.user_id`를 전달한다.
- 클라이언트가 보낸 user id는 신뢰하지 않는다.

### 문서 검색 범위

- `RagRepository.search_chunks_hybrid()` 시그니처를 변경한다.
  - 현재: `transcript_id: UUID | None`
  - 변경: `transcript_ids: list[UUID]`
- `_search_by_keyword()`와 `_search_by_vector()`도 동일하게 변경한다.
- SQL 조건:

```sql
sc.transcript_id = ANY($n::uuid[])
```

- user 소유권 필터는 기존처럼 `JOIN transcripts t ON sc.transcript_id = t.id` 후 `t.user_id = $n` 조건으로 유지한다.

### Parent Hydration

- 기존 `get_parent_chunks(parent_chunk_ids)`는 유지한다.
- source 응답에 transcript title이 필요하면 다음 중 하나를 선택한다.
  - v1 간단안: `title = chunk.topic or "강의 자료"`
  - 개선안: parent chunk 조회 시 `transcripts.title`을 JOIN해 함께 가져온다.
- 이번 구현에서는 가능하면 JOIN 방식으로 `transcript_title`을 가져와 `RetrievedSource.title`에 사용한다.

### RetrievedSource 변환

- `RagQueryService.search()`는 더 이상 `ParentChunkResult`를 그대로 반환하지 않는다.
- 검색 hit score를 parent chunk로 전달하기 위해 parent id 기준 score map을 유지한다.
- 변환 규칙:
  - `source_type`: `"document"`
  - `title`: `transcript_title` 또는 `topic` 또는 `"강의 자료"`
  - `snippet`: `summary`가 있으면 summary, 없으면 text 앞부분
  - `transcript_id`: chunk의 transcript_id
  - `url`: `null`
  - `score`: 검색 hit score
  - `metadata`: topic, keywords, start_seconds, end_seconds, segment range 등

### LLM 답변

- `RagResponseService.generate()` 입력을 `list[RetrievedSource]`로 변경한다.
- 프롬프트는 문서 source 전용으로 유지한다.
- 답변 규칙:
  - 한국어
  - 3~5문장 이내
  - source에 없는 내용 추측 금지
  - 근거가 부족하면 “제공된 강의 자료에는 해당 내용이 없습니다.”라고 답변

## Test Plan

- 요청 검증:
  - body에 `user_id` 없이 JWT 사용자로 검색한다.
  - `transcript_ids=[]`는 422.
- 인증:
  - route가 `get_current_user`의 user_id를 service에 전달하는지 검증한다.
- repository:
  - keyword 검색 SQL에 `sc.transcript_id = ANY($n::uuid[])`가 포함되는지 검증한다.
  - vector 검색 SQL도 동일하게 검증한다.
  - user_id 조건이 있을 때 `JOIN transcripts t`와 `t.user_id`가 포함되는지 검증한다.
- service:
  - 여러 transcript_ids로 검색해 parent chunk가 score 순으로 정렬되는지 검증한다.
  - `ParentChunkResult`가 `RetrievedSource`로 변환되는지 검증한다.
- response:
  - `sources`가 `source_type`, `title`, `snippet`, `transcript_id`, `url`, `score`만 클라이언트 핵심 필드로 포함하는지 검증한다.
  - 검색 결과가 없으면 answer는 안내 문구, sources는 빈 배열.

## Out Of Scope

- Tavily 웹 검색
- `scope: "web" | "hybrid"`
- document/web ranking 병합
- BGE reranker 또는 로컬 reranker 모델
- 웹 URL 기반 citation

위 항목은 다음 단계 `server/plan/rag_web_hybrid.md`에서 다룬다.

## Assumptions

- 이번 변경은 `/rag/query` 요청/응답 스키마의 breaking change다.
- 현재 서비스의 모든 검색 대상은 강의 transcript로 간주한다.
- 검색 대상 transcript는 반드시 인증 사용자 소유여야 한다.
