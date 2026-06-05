# RAG Web/Hybrid Session Summary

작성 시각: 2026-06-04 23:22:30

## 요약

이번 세션에서는 실시간 녹음 저장 오류 원인 분석, OpenAI/Deepgram 비용 및 호출 흐름 점검, RAG web/hybrid 검색 기능 구현, Tavily 의존성 설치 문제 해결, WebSocket 403 원인 분석, 그리고 구현 리뷰 기록을 진행했다.

## 진행 내용

### 1. `domain_type` DB 오류 분석

- `asyncpg.exceptions.NotNullViolationError` 원인은 코드가 아니라 실제 DB 스키마에 `transcripts.domain_type NOT NULL` 컬럼이 남아 있는 상태로 판단했다.
- 현재 코드의 `TranscriptCreate`, `RagRepository.create_transcript()`, realtime 저장 요청 모델에는 `domain_type`이 남아 있지 않음을 확인했다.
- `server/db/migrations/008_remove_domain_type_add_lecture_summaries.sql`에 잘못 들어간 `active :` 줄을 제거했다.
- 해결 방향으로 008 마이그레이션 적용 또는 `ALTER TABLE transcripts DROP COLUMN IF EXISTS domain_type` 실행을 안내했다.

### 2. 비용 및 realtime 호출 흐름 점검

- 5분 음성 파일 5~6회 transcript 요청만으로 팀 사용량 90달러가 나오기는 어렵다고 판단했다.
- 기본 설정 기준 `whisper-1`, `gpt-4o-mini`, `text-embedding-3-small` 호출 비용은 낮은 편임을 설명했다.
- 비용 위험 후보로 비싼 모델 override, 반복 PDF 요약, 실시간 요약 장시간 실행, 다른 팀원/공유 키 사용을 정리했다.
- 실시간 요약은 transcript final event가 들어올 때만 flush 판단을 하므로, 데이터가 없을 때 타이머처럼 계속 요청하지 않는다고 확인했다.
- Deepgram도 클라이언트가 오디오 chunk를 보낼 때만 `send_audio()`가 호출되는 구조라고 설명했다.

### 3. RAG Web/Hybrid 구현

- `server/plan/rag_web_hybrid.md`와 `README.md`를 참고해 `/rag/query`에 `scope`를 추가했다.
- `scope` 값:
  - `document`: 기존 문서 RAG 검색
  - `web`: Tavily 웹 검색
  - `hybrid`: 문서 검색 + 웹 검색 병합
- `RetrievedSource.source_type`을 `"document" | "web"`로 확장했다.
- `TAVILY_API_KEY`, `WEB_SEARCH_MAX_RESULTS` 설정을 추가했다.
- `WebSearchService`를 추가해 Tavily 결과를 `RetrievedSource`로 정규화했다.
- `RerankService` 프로토콜과 `IdentityRerankService`를 추가했다.
- `RagResponseService`가 web source URL을 LLM context에 포함하도록 보강했다.
- `tavily-python` 의존성을 `pyproject.toml`에 추가하고 `uv.lock`을 갱신했다.

### 4. 테스트 및 의존성 설치

- 관련 테스트를 추가/수정했다.
  - `test_rag_routes.py`
  - `test_web_search_service.py`
  - `test_rerank_service.py`
- 관련 테스트 실행 결과:
  - `43 passed`
- 이후 실제 실행 환경에서 `tavily-python is required for web search.` 502가 발생해 `server/.venv`에 패키지가 설치되지 않은 것을 확인했다.
- `uv sync`를 실행해 `tavily-python==0.7.25`와 관련 의존성을 설치했다.
- `AsyncTavilyClient` import 확인 및 `test_web_search_service.py` 통과를 확인했다.

### 5. WebSocket 403 분석

- `WebSocket /audio/realtime/connect?token=... 403` 로그는 Deepgram 연결 전 단계에서 JWT 검증 실패로 판단했다.
- 서버 재시작 후 기존 access token이 무효화되었거나, 프론트가 오래된 토큰을 쓰는 상황을 주요 원인으로 설명했다.
- 새 로그인 후 새 access token을 WebSocket query param에 넣어야 한다고 안내했다.

### 6. 코드 리뷰 기록

- uncommitted changes 리뷰를 수행했다.
- 발견 사항:
  - `scope="web"`이어도 document RAG dependency가 먼저 생성됨
  - Tavily snippet이 LLM context와 프론트 응답에 그대로 길게 전달됨
  - reranker가 이미 `top_k`로 잘린 후보만 받음
- 리뷰 내용을 `server/review/2026-06-04_23-19-16_rag_web_hybrid_review.md`에 저장했다.

## 남은 확인 사항

- 실제 DB에 008 마이그레이션 적용 여부 확인
- 서버 재시작 후 Tavily web/hybrid 검색 정상 동작 확인
- `scope="web"` 경로가 document RAG dependency를 만들지 않도록 개선
- web snippet 길이 제한 또는 LLM context용/프론트 표시용 source 분리
- rerank 전에 후보를 `top_k`로 자르지 않도록 개선

rag-web 작업 마치