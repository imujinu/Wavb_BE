# RAG Web/Hybrid 리뷰 수정 플랜

## Summary

- `scope="web"` 요청에서 DB/RAG/embedding 의존성이 생성되지 않도록 `RagQueryService`만 lazy 생성한다.
- Tavily `content`는 `WEB_SEARCH_SNIPPET_MAX_CHARS` 설정값으로 잘라 LLM context 비용과 응답 크기를 제한한다.
- hybrid에서는 document/web 후보를 `top_k`로 먼저 자르지 않고, 전체 후보를 reranker에 넘긴 뒤 마지막에만 `top_k`를 적용한다.

## Key Changes

- `server/routes/rag.py`
  - `rag_query()` 시그니처에서 `rag_query_service: Depends(get_rag_query_service)` 제거.
  - `get_rag_query_service_factory()`를 추가하고, document/hybrid 분기 안에서만 async context manager로 `RagQueryService`를 생성한다.
  - `_select_sources()`는 scope별 후보 정규화/병합만 수행하고 hybrid에서 `ranked[:top_k]`를 하지 않는다.
  - 최종 반환 후보 제한은 기존처럼 `rerank_service.rerank(...)` 이후 `sources[:request.top_k]` 한 곳에서만 수행한다.

- `server/services/rag/web_search_service.py`
  - `WebSearchService.__init__()`에서 `settings.web_search_snippet_max_chars`를 읽는다.
  - Tavily `content`를 공백 정규화 후 설정값 기준으로 trim하고, 초과 시 `...`를 붙인다.
  - snippet이 비어 있고 URL도 없으면 skip하는 현재 정책은 유지한다.

- `server/settings.py` / `server/.env.example`
  - `web_search_snippet_max_chars: int = Field(800, alias="WEB_SEARCH_SNIPPET_MAX_CHARS")` 추가.
  - `.env.example`에는 `WEB_SEARCH_MAX_RESULTS` 근처에 `WEB_SEARCH_SNIPPET_MAX_CHARS=800` 추가하되, 현재 작업트리의 기존 변경은 보존한다.

## Public Interfaces

- 새 환경변수: `WEB_SEARCH_SNIPPET_MAX_CHARS`
  - 기본값: `800`
  - 의미: 웹 검색 결과의 `RetrievedSource.snippet` 최대 문자 수.
- `/rag/query` 요청/응답 스키마는 변경하지 않는다.
- `RetrievedSource` 스키마도 변경하지 않는다.

## Test Plan

- `server/tests/test_rag_routes.py`
  - `scope="web"` 요청에서 lazy factory가 호출되지 않는 회귀 테스트 추가.
  - document/hybrid 요청에서는 lazy factory가 호출되고, 검색 인자 `query`, `transcript_ids`, `user_id`, `top_k`가 유지되는지 확인.
  - hybrid에서 document/web 후보가 reranker에 모두 전달되고, rerank 이후에만 `top_k`로 잘리는 테스트 추가.

- `server/tests/test_web_search_service.py`
  - 긴 Tavily `content`가 `WEB_SEARCH_SNIPPET_MAX_CHARS` 기준으로 trim되는 테스트 추가.
  - 짧은 snippet은 그대로 유지되는 기존 mapping 테스트 보강.
  - 기존 `WEB_SEARCH_MAX_RESULTS` cap 테스트 유지.

- 검증 명령:
  - `uv run pytest tests/test_rag_routes.py tests/test_web_search_service.py`
  - 가능하면 전체 회귀로 `uv run pytest`

## Assumptions

- lazy 범위는 사용자 선택대로 `RagQueryService`에만 적용하고, `RagResponseService` 생성 시점은 이번 수정에서 바꾸지 않는다.
- hybrid 후보 수는 각 retriever가 현재 반환하는 후보를 그대로 사용하며, 별도의 `candidate_k` 설정은 이번 범위에 추가하지 않는다.
- 웹 snippet은 별도 raw/full content 필드를 추가하지 않고 현재 응답 필드인 `snippet` 자체를 제한한다.
