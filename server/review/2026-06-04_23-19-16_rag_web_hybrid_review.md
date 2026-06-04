# RAG Web/Hybrid 변경 리뷰

작성 시각: 2026-06-04 23:19:16

## 발견 사항

### 1. `scope="web"`이어도 문서 검색 의존성이 먼저 생성됨

[server/routes/rag.py](../routes/rag.py)에서 `rag_query_service`가 FastAPI dependency로 선언되어 있어서, 요청이 web-only여도 endpoint 실행 전에 DB/RAG/embedding 관련 객체가 먼저 만들어진다.

그 결과 순수 웹 검색 요청인데도 DB 연결이나 document RAG 쪽 설정 문제로 실패할 수 있다.

고치는 방향:

- `scope` 확인 후 document/hybrid일 때만 `RagQueryService`를 만들도록 lazy 생성
- 또는 `/rag/query` 내부에서 connection/repository를 직접 분기 생성

### 2. Tavily snippet이 너무 길게 LLM과 프론트로 그대로 감

[server/services/rag/web_search_service.py](../services/rag/web_search_service.py)에서 Tavily `content`를 그대로 `RetrievedSource.snippet`에 넣고 있다.

이 `sources`가 그대로 `RagResponseService.generate()`에 들어가 LLM prompt context가 되므로, `top_k`가 커지거나 snippet이 길면 토큰 비용, 지연 시간, 응답 크기가 늘어날 수 있다.

고치는 방향:

- web snippet을 500~1000자 정도로 제한
- 더 좋게는 LLM용 context와 프론트 표시용 snippet을 분리

### 3. reranker가 후보 전체가 아니라 이미 잘린 `top_k`만 받음

[server/routes/rag.py](../routes/rag.py)에서 `_select_sources()`가 hybrid 후보를 이미 `top_k`로 잘라서 반환하고, 그 뒤에 reranker를 호출한다.

지금 identity reranker에서는 문제가 없지만, 나중에 BGE reranker 같은 실제 reranker를 붙이면 `top_k` 밖 후보를 다시 올릴 기회가 없어진다.

고치는 방향:

- hybrid에서는 document + web 후보 전체를 정규화해서 반환
- rerank 후 마지막에 `sources[:top_k]` 적용

## 요약

당장 치명적인 버그라기보다는, web-only 안정성, 토큰 비용/응답 크기, 향후 reranker 확장성 쪽에서 손보면 좋은 부분들이다.
