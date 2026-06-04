# RAG Web/Hybrid 검색 확장 설계

## Summary

- 이 문서는 `rag-v2.md`의 문서 검색 정리가 끝난 다음 단계에서 수행한다.
- `/rag/query`에 웹 검색과 문서+웹 hybrid 검색을 추가한다.
- 문서 검색은 `rag-v2.md`에서 정리한 `RetrievedSource` 기반 응답 모델을 재사용한다.
- 웹 검색은 Tavily Python SDK의 async client를 사용한다.
- BGE reranker는 이번 단계에서도 실제 모델 연결은 하지 않고, 교체 가능한 인터페이스와 identity 구현만 둔다.

## Prerequisites

- `rag-v2.md` 완료:
  - JWT 기반 RAG 인증 적용
  - `transcript_ids: UUID[]` 문서 검색
  - `RetrievedSource` 응답 모델
  - `RagResponseService.generate(query, sources)` 구조

## API Changes

- `POST /rag/query` 요청 모델에 `scope`를 추가한다.

```json
{
  "query": "질문",
  "scope": "hybrid",
  "transcript_ids": ["uuid"],
  "top_k": 5
}
```

- `scope`
  - `"document"`: 사용자 소유 transcript만 검색
  - `"web"`: 웹 검색만 수행
  - `"hybrid"`: 문서 검색과 웹 검색을 모두 수행 후 병합
- `transcript_ids`
  - `document`, `hybrid`: 1개 이상 필수
  - `web`: 생략 가능
- 응답 모델은 `rag-v2.md`의 `RagQueryResponse`를 유지한다.
  - `sources[].source_type`은 `"document"` 또는 `"web"`이 될 수 있다.
  - `warnings`에는 부분 실패 메시지를 담는다.

## Settings And Dependencies

- `pyproject.toml`
  - `tavily-python` 추가
- `settings.py`
  - `tavily_api_key: str = Field("", alias="TAVILY_API_KEY")`
  - `web_search_max_results: int = Field(5, alias="WEB_SEARCH_MAX_RESULTS")`
- `TAVILY_API_KEY`가 없을 때:
  - `scope="web"`: 502
  - `scope="hybrid"`: document 결과가 있으면 warning 포함 후 document-only로 계속 진행

## Web Search Service

- 새 파일: `services/rag/web_search_service.py`
- 역할:
  - Tavily async client 호출
  - Tavily 결과를 `RetrievedSource`로 정규화
- 호출 기준:
  - `max_results=5`
  - `include_answer=False`
  - `include_raw_content=False`

### Tavily Result Mapping

- Tavily result → `RetrievedSource`
  - `source_type`: `"web"`
  - `title`: result title
  - `snippet`: result content
  - `url`: result url
  - `score`: result score
  - `transcript_id`: null
  - `metadata`: provider, published date 등 사용 가능한 부가 정보

## Hybrid Flow

1. request 검증
2. `scope=document`
   - 기존 document retrieval만 수행
3. `scope=web`
   - web retrieval만 수행
4. `scope=hybrid`
   - document retrieval 수행
   - web retrieval 수행
   - web 실패 시 document 결과가 있으면 warning 추가 후 계속
   - 둘 다 실패 또는 결과 없음이면 빈 source로 답변 생성
5. source score 정규화
6. reranker 호출
7. 상위 `top_k`만 LLM context와 응답에 사용

## Score Merge

- v1에서는 단순 정규화만 사용한다.
- document score:
  - RRF score는 값 범위가 작으므로 source 목록 내 min-max 또는 max 기준으로 0~1 정규화한다.
- web score:
  - Tavily score가 0~1이라고 가정하되, 범위를 벗어나면 0~1로 clamp한다.
- 같은 점수일 때 우선순위:
  1. document
  2. web

## Rerank Service

- 새 파일: `services/rag/rerank_service.py`
- 인터페이스:

```python
class RerankService(Protocol):
    async def rerank(self, query: str, sources: list[RetrievedSource]) -> list[RetrievedSource]:
        ...
```

- v1 구현:
  - `IdentityRerankService`
  - 입력 순서 그대로 반환
- 목적:
  - 추후 BGE reranker, FlagEmbedding, sentence-transformers 등으로 교체할 지점을 미리 만든다.

## RagResponseService Changes

- document/web 공통 source를 context로 받는다.
- 프롬프트 규칙:
  - 한국어
  - 핵심만 3~5문장
  - source에 없는 내용 추측 금지
  - document source와 web source를 구분해 근거로 사용
  - 웹 source의 URL은 context에 포함하되 답변 본문은 간결하게 유지

## Error Policy

- `scope=web`
  - Tavily 키 없음: 502
  - Tavily 호출 실패: 502
- `scope=hybrid`
  - document 검색 성공, Tavily 실패: 200 + warnings
  - document 결과 없음, Tavily 성공: 200
  - document 결과 없음, Tavily 실패: 502 또는 200 + 빈 sources 중 하나를 선택해야 함
    - 권장: 502. hybrid에서 사용할 수 있는 source가 하나도 없기 때문

## Test Plan

- 요청 검증:
  - `scope=document`에서 transcript_ids 누락 시 422
  - `scope=hybrid`에서 transcript_ids 누락 시 422
  - `scope=web`에서 transcript_ids 생략 가능
- WebSearchService:
  - Tavily 결과를 `RetrievedSource(source_type="web")`로 정규화한다.
  - Tavily 키 미설정 시 예외 처리.
  - Tavily 호출 실패 시 예외 처리.
- Hybrid:
  - document + web 결과를 병합하고 top_k만 반환한다.
  - Tavily 실패 시 document 결과와 warning을 반환한다.
  - score 정규화와 tie-break가 안정적으로 동작한다.
- Rerank:
  - `IdentityRerankService`가 입력 순서를 유지한다.
  - hybrid flow에서 reranker가 호출된다.
- Response:
  - sources는 document/web 모두 동일한 `RetrievedSource` 스키마로 내려간다.
  - warnings는 Tavily 부분 실패 시에만 포함된다.

## Assumptions

- 웹 검색은 강의 transcript에 없는 최신/외부 지식을 보강하기 위한 선택 기능이다.
- 기본 scope는 `"document"`로 둔다.
- 웹 검색 결과는 저장하지 않는다.
- Tavily raw content는 v1에서 사용하지 않는다.
