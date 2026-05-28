# RAG Chatbot LangGraph Plan

## Summary
- 요청한 구조는 현재 repo의 `transcripts → chunks(parent) → search_chunks(child)` 저장 구조와 잘 맞으므로 타당합니다.
- v1은 **LangGraph 기반 RAG 챗봇 API**를 추가하되, Tavily는 실제 외부 호출이 아니라 `web_search` 노드와 `tavily_web_search` tool 인터페이스만 준비합니다.
- `LangGraph interrupt`를 실제로 사용하므로 PostgreSQL checkpointer를 사용합니다. LangGraph 공식 문서상 interrupt/resume 같은 human-in-the-loop 흐름에는 checkpoint persistence가 필요하고, PostgreSQL checkpointer 패키지는 `langgraph-checkpoint-postgres`로 제공됩니다. ([docs.langchain.com](https://docs.langchain.com/oss/python/langgraph/interrupts?utm_source=openai))
- 계획 문서는 구현 시 `server/plan/rag-chatbot-langgraph.md`로 생성합니다.

## Step Plan
1. **기반 의존성/설정 추가**
   - 작업: `langgraph`, `langgraph-checkpoint-postgres` 의존성 추가, RAG 설정값 추가.
   - 설정: `RAG_SEARCH_TOP_K=12`, `RAG_PARENT_TOP_K=5`, `RAG_MIN_CONFIDENCE=0.35`, `RAG_MAX_CONTEXT_CHARS=6000`, `WEB_SEARCH_API_KEY`는 예약만.
   - 이유: top-k는 keyword/vector 후보를 충분히 확보하면서 parent hydrate 비용을 제한하고, confidence threshold는 낮은 근거 답변을 interrupt로 돌리기 위한 기준입니다.

2. **Public API 추가**
   - 작업: `POST /rag/chat`, `POST /rag/chat/resume` 추가.
   - `POST /rag/chat` request:
     ```json
     {
       "query": "질문",
       "transcript_id": "optional uuid",
       "user_id": "optional uuid",
       "domain_type": "meeting | lecture | optional",
       "conversation_id": "optional uuid"
     }
     ```
   - 검증: `transcript_id` 또는 `user_id` 중 최소 하나는 필수. 둘 다 있으면 둘 다 필터로 사용.
   - interrupt response:
     ```json
     {
       "status": "interrupted",
       "thread_id": "uuid",
       "reason": "low_confidence",
       "message": "질문을 조금 더 구체화해 주세요.",
       "suggested_queries": []
     }
     ```
   - completed response:
     ```json
     {
       "status": "completed",
       "answer": "...",
       "confidence": 0.0,
       "sources": []
     }
     ```

3. **Repository 검색 기능 추가**
   - 작업: `search_chunks` 대상 keyword/vector/hybrid 검색 메서드와 parent chunk hydrate 메서드 추가.
   - keyword search: `search_chunks.text`를 `to_tsvector('simple', text)` 기반으로 검색하고, 필요하면 `003` migration에서 `idx_search_chunks_text_fts` 추가.
   - vector search: query embedding 생성 후 `embedding <=> $vector` cosine distance 정렬.
   - hybrid search: keyword/vector 결과를 reciprocal-rank 방식으로 병합. 기본은 hybrid.
   - 이유: child chunk는 검색 단위, parent chunk는 답변 맥락 단위이므로 “child 검색 → parent hydrate” 구조를 유지합니다.

4. **LangGraph 노드/툴 구현**
   - `query_processor`: 질문 정규화, 검색 범위 검증, 모호성/짧은 질문 감지.
   - `search_documents`: 검색 가능한 transcript/search_chunk 존재 여부와 초기 confidence 계산.
   - 낮은 confidence면 `interrupt()` 호출 후 `/rag/chat/resume`에서 `Command(resume=...)`로 재개합니다. LangGraph interrupt는 상태 저장 후 resume input으로 재개하는 방식입니다. ([docs.langchain.com](https://docs.langchain.com/oss/python/langgraph/interrupts?utm_source=openai))
   - `search_router`: factual/keyword형은 keyword, 개념형은 vector, 일반 질의는 hybrid로 라우팅.
   - tools: `keyword_search`, `vector_search`, `hybrid_search`, `get_parent_chunks`, `tavily_web_search`.
   - `search_parent_chunks`: child hit의 `parent_chunk_id`로 parent `chunks` 조회.
   - `rerank`: v1은 deterministic rerank. child score, parent metadata keyword match, transcript/domain filter match를 합산.
   - `check_context`: 점수/문맥 길이 기준으로 충분 여부 판단.
   - `web_search`: v1에서는 Tavily disabled result만 state에 기록.
   - `generate_response`: 내부 근거만 사용해 답변하고, 부족하면 “내부 자료에서 충분한 근거를 찾지 못함”을 명시.

5. **응답 근거와 안전장치**
   - sources에는 `transcript_id`, `parent_chunk_id`, `child_index`, `start_seconds`, `end_seconds`, `score`, `snippet` 포함.
   - 답변 생성 prompt는 제공된 context 밖의 내용을 단정하지 않도록 제한.
   - web_search가 disabled인 v1에서는 외부 검색 결과를 답변 근거로 사용하지 않습니다.

## Test Plan
- Repository tests:
  - keyword/vector/hybrid SQL이 올바른 필터와 top-k를 사용.
  - `transcript_id`, `user_id`, `domain_type` 필터 조합 검증.
  - parent hydrate가 중복 parent를 제거하고 score 순서를 보존.
- Graph tests:
  - 낮은 confidence에서 interrupt 발생.
  - resume 입력 후 search_router부터 정상 진행.
  - hybrid route가 child hit → parent chunks → rerank → answer로 연결.
  - context 부족 시 web_search node가 disabled marker를 남기고 hallucination 없이 응답.
- API tests:
  - `/rag/chat` completed 응답.
  - `/rag/chat` interrupted 응답.
  - `/rag/chat/resume` 재개 응답.
  - `transcript_id`/`user_id` 누락 시 422.
- Regression:
  - 기존 audio upload, search_chunks 저장 테스트는 그대로 통과해야 함.

## Assumptions
- v1 검색 범위는 `transcript_id` 단일 검색과 `user_id` 전체 검색을 모두 지원합니다.
- LangGraph checkpoint는 PostgreSQL을 사용하고 기존 `DATABASE_URL` 기반으로 구성합니다.
- Tavily는 v1에서 실제 호출하지 않고 interface/stub만 둡니다.
- rerank는 v1에서 LLM reranker나 cross-encoder 없이 deterministic score로 구현합니다.
- 구현 시 계획 파일 `server/plan/rag-chatbot-langgraph.md`를 추가합니다.
