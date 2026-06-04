  # 강의 요약 데이터 기능 및 domain_type 제거 설계

  ## Summary

  - domain_type은 API, DB, schema, repository, chunk/summary 분기에서 제거한다.
  - 모든 녹음/전사 데이터는 강의 기준으로 처리한다.
  - PDF 템플릿 기능은 유지하되, 새 “요약 데이터” 기능과 분리한다.
  - 새 요약 데이터는 전체 요약, 맥락 단위 섹션, 키워드별 관련 내용을 담는 전용 JSON으로 생성하고 별도 테이블에 저장한
    다.

  ## Key Changes

  - 업로드 API에서 domain_type form 입력과 validation을 제거한다.
  - DB migration을 추가해 transcripts.domain_type, chunks.domain_type, 관련 CHECK 제약과 인덱스를 제거한다.
  - fresh DB용 기존 초기 migration도 최종 스키마와 맞도록 정리한다.
  - TranscriptCreate, TranscriptDetail, ChunkCreate, ParentChunkResult 등 Pydantic 모델에서 domain_type 필드를 제거한
    다.
  - chunk 생성/metadata/planning은 강의 전용으로 단순화한다.
      - meeting builder, meeting prompt, decision/action item metadata 분기 제거.
      - chunk strategy는 예: lecture_context_plan_v1, fallback은 lecture_context_fallback_v1로 고정.
      - metadata는 concepts, learning_points, summary_hint 중심으로 유지한다.
  - PDF 템플릿 레지스트리와 /summary-pdf 기능은 유지한다.
      - 단, PDF 생성 시 domain_type 힌트 전달은 제거한다.

  ## Summary Data API

  - 새 endpoint: POST /audio/transcripts/{transcript_id}/summary
  - 인증된 사용자 소유 transcript만 처리한다.
  - transcript가 없으면 404, status != completed 또는 full_text 공백이면 409.
  - 이미 생성된 강의 요약 데이터가 있으면 LLM 재호출 없이 기존 데이터를 반환한다.
  - 응답:

  {
    "summary_id": "uuid",
    "transcript_id": "uuid",
    "payload": {
      "overview": {
        "title": "강의 제목",
        "summary": "전체 맥락 요약",
        "key_points": ["핵심 요점"]
      },
      "contexts": [
        {
          "index": 0,
          "subtitle": "소제목",
          "content": "해당 맥락 단위의 설명",
          "start_seconds": 0,
          "end_seconds": 120,
          "segment_start_index": 0,
          "segment_end_index": 8
        }
      ],
      "keywords": [
        {
          "keyword": "핵심어",
          "summary": "강의에서 이 키워드가 다뤄진 내용",
          "related_context_indices": [0, 2]
        }
      ]
    }
  }

  ## Storage And Flow

  - 새 테이블 예: lecture_summaries
      - id UUID PK
      - transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE
      - user_id UUID
      - payload JSONB NOT NULL
      - model TEXT
      - created_at, updated_at
  - 인덱스:
      - (transcript_id)
      - (user_id, created_at DESC)
  - 생성 흐름:
      - transcript 조회
      - transcript의 저장된 chunks를 순서대로 조회
      - chunks의 text, topic, summary, keywords, metadata.concepts, metadata.learning_points, time/segment range를 입
        력으로 사용
      - LLM이 overview, contexts, keywords를 생성
      - payload를 정규화해 누락 필드는 빈 문자열/빈 배열로 보정
      - lecture_summaries에 저장 후 반환
  - chunks가 없는 예외 상황은 409로 처리한다. 요약 데이터는 “맥락 단위 기반” 기능이므로 chunk pipeline 완료 후 생성한
    다.

  ## Test Plan

  - domain_type 제거:
      - chunk builder/planner/metadata 테스트가 강의 전용 기준으로 갱신된다.
      - migrations에서 domain_type 컬럼과 관련 인덱스 제거를 검증한다.
  - 요약 API:
      - completed transcript + chunks 있음 → 새 요약 생성 및 저장.
      - 기존 summary 있음 → LLM/insert 호출 없이 기존 payload 반환.
      - transcript 없음/비소유 → 404.
      - processing/failed transcript → 409.
      - full_text 공백 → 409.
      - chunks 없음 → 409.
      - payload에 overview, contexts, keywords가 항상 포함되는지 검증.
  - PDF 기능:
      - 기존 /audio/summary-templates, /audio/transcripts/{id}/summary-pdf, /audio/summary-documents/{id} 테스트는 유
        지하되 domain_type 의존만 제거한다.

  ## Assumptions

  - 앞으로 서비스의 전사 데이터는 모두 강의로 간주한다.
  - PDF 템플릿은 “특정 PDF 양식 채우기” 기능이고, 새 요약 데이터 API와 저장소를 공유하지 않는다.
  - 요약 데이터의 source of truth는 새 lecture_summaries.payload이며, transcripts.summary는 이번 변경에서 사용하지 않
    는다.