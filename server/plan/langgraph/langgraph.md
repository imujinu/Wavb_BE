  # Persona-Aware Routed RAG 설계 계획

  ## Summary

  - 기존 POST /rag/query를 하위 호환으로 확장해, 기존 scope=document|web|hybrid 요청은 그대로 동작시키고 새 요청에는
    LLM Router 기반 concept/location/compare/external/exam/problem 분기를 적용한다.

  - 오디오 파일은 STT 완료 후 별도 transcript_personas 테이블에 페르소나를 저장하고, 질문 응답 생성 시 선택된
    transcript가 오디오이면 해당 페르소나를 system prompt에 반영한다.

    요구한 “step 단위, 필요성, 작업 요약, 동작 흐름, 설정 이유” 형식을 따른다.

  ## Key Changes

  - API/schema:
      - RagQueryRequest에 선택 필드 추가: route_mode: "auto"|"legacy" = "auto", intent: concept|location|compare|
        external|exam|problem|null, quality_threshold: float = 0.65.

      - RagQueryResponse에 intent, quality, used_persona, fallback_attempts를 선택 필드로 추가해 기존 클라이언트 응답
        을 깨지 않는다.

      - 기존 scope는 유지한다. route_mode="legacy" 또는 명시 scope 기반 요청은 현재 흐름을 그대로 사용한다.

  - DB/persona:
      - 새 마이그레이션으로 transcript_personas 테이블 추가: id, transcript_id UNIQUE, user_id, persona_type, tone,
        expertise_level, speaking_style, summary, raw_payload JSONB, model, timestamps.

      - RagRepository에 upsert_transcript_persona(), get_transcript_persona(), get_transcript_source_types()를 추가한
        다.

      - 오디오 처리 완료 시점인 TranscriptProcessingService.process_content() 또는 즉시 STT 경로인
        TranscriptIngestionService.ingest_upload() 직후 PersonaExtractionService를 호출한다. 비오디오에는 persona row
        를 만들지 않는다.

  - Services:
      - QuestionRouterService: gpt-4o-mini로 질문을 concept/location/compare/external/exam/problem 중 하나로 분류하고,
        실패 시 concept로 fallback한다.

      - RoutedRetrievalService: intent별 검색 전략을 캡슐화한다.
          - concept: 기존 벡터+FTS 유사도 검색.
          - location: source_type, page/slide/time metadata 기반 필터 검색을 우선하고 부족하면 concept 검색 보강.
          - compare: 여러 transcript_ids를 파일별로 검색한 뒤 파일별 근거를 균형 있게 병합.
          - external: Tavily 웹 검색.
          - exam: lecture_summaries가 있으면 contexts/keywords를 우선 사용하고, 없으면 chunk 수집 후 요약 생성.
          - problem: concept 검색과 예시/문제 키워드 검색을 병렬 실행 후 병합.

      - PersonaAwareResponseService: 기존 RagResponseService를 확장하거나 래핑해 persona prompt, intent별 답변 지침,
        source context를 함께 전달한다.

      - AnswerGradingService: 답변 품질을 groundedness, coverage, clarity 기준 JSON으로 평가하고 score <
        quality_threshold이면 fallback branch를 1회만 재시도한다.

  ## Step Plan

  1. 현재 RAG 계약 보존
      - 필요성: 기존 /rag/query, 테스트, 프론트 호출을 깨지 않기 위함.
      - 작업: 기존 scope 흐름을 legacy 경로로 분리하고, 새 auto route 경로를 같은 endpoint 내부 오케스트레이터로 연결
        한다.

  2. 페르소나 저장 기반 추가
      - 필요성: 질문 시점마다 오디오 전체를 다시 분석하지 않고 파일별 발화 톤을 재사용하기 위함.
      - 작업: transcript_personas 마이그레이션, Pydantic schema, repository CRUD, 오디오 STT 완료 후 추출 서비스를 추
        가한다.

  3. Router와 intent별 검색 전략 구현
      - 필요성: 질문 유형마다 최적 retrieval이 다르므로 단일 vector search보다 답변 품질을 높일 수 있음.
      - 작업: QuestionRouterService와 RoutedRetrievalService를 추가하고 기존 RagQueryService, WebSearchService,
        LectureSummaryService를 재사용한다.

  4. Persona Check와 Generate 통합
      - 필요성: 오디오 기반 답변은 원 발화자의 톤/관점을 반영하고, 문서/PDF/PPT는 기본 톤을 유지해야 함.
      - 작업: 검색된 source 또는 요청 transcript_ids의 source_type을 확인해 오디오면 persona를 로드하고, 없으면
        general 기본 톤으로 생성한다.

  5. Grade와 fallback 추가
      - 필요성: 라우팅 실패나 검색 부족 시 빈약한 답변을 그대로 반환하지 않기 위함.
      - 작업: 품질 점수가 기준 미달이면 intent별 fallback map을 적용한다. 기본 fallback은 external -> concept,
        location -> concept, problem -> concept, exam -> concept, compare -> concept, concept -> hybrid로 한다.

  ## Final Flow

  - 업로드 시점:
      - audio upload/process → STT → chunks/index 생성 → PersonaExtractionService → transcript_personas 저장.
      - pdf/ppt upload/process → text extraction → chunks/index 생성 → persona 없음.

  - 질문 시점:
      - /rag/query 요청 → QuestionRouterService intent 분류 → intent별 retrieval → source transcript의 오디오 여부 확
        인 → persona 로드 또는 기본 톤 선택 → 답변 생성 → 품질 평가 → 필요 시 fallback 1회 → 최종 응답.

  ## Test Plan

  - Router:
      - 각 intent 분류 결과가 올바른 retrieval branch를 호출한다.
      - LLM router 실패 시 concept fallback을 사용한다.

  - Persona:
      - persona가 없는 오디오도 실패하지 않고 general 톤으로 답변한다.

  - Retrieval:
      - concept는 기존 RagQueryService.search()를 호출한다.
      - external은 Tavily 실패 정책을 기존 web/hybrid 정책과 일관되게 처리한다.
      - compare는 여러 transcript 결과를 한 파일에 치우치지 않게 반환한다.
      - exam은 기존 lecture_summaries가 있으면 LLM 재요약 없이 사용한다.

  - Grade/fallback:
      - 충분한 답변은 fallback 없이 반환한다.
      - 낮은 점수는 fallback을 1회만 수행한다.
      - fallback 후에도 낮으면 warnings에 품질 경고를 담고 최선 답변을 반환한다.

  ## Assumptions

  - API는 사용자가 선택한 대로 기존 /rag/query 확장 방식으로 설계한다.
  - 페르소나는 사용자가 선택한 대로 별도 transcript_personas 테이블에 저장한다.
  - Router/Persona/Grade 모델은 기존 설정과 맞춰 기본 gpt-4o-mini를 사용한다.
  - fallback 재시도는 비용과 지연을 제한하기 위해 최대 1회로 둔다.
  - PLAN.md 자체는 인코딩 손상 가능성이 있으므로 직접 수정하지 않고 새 계획 문서를 만든다.