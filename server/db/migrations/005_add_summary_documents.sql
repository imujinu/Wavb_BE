-- Active: 1780363895240@@127.0.0.1@5432@recordoc
-- summary_documents 테이블 생성: 스크립트 + 템플릿으로 생성한 요약 결과(구조화 payload)를 영속화한다.
--
-- 설계 이유:
--   payload(JSONB): LLM이 생성한 섹션별 구조화 요약을 그대로 보관한다.
--     "PDF 내용이 잘못되면 수정해서 다시 만든다" 요구를 위해, 저장된 payload를 수정한 뒤
--     LLM 재호출 없이 PDF만 재렌더할 수 있다.
--   template_id(TEXT): 코드 레지스트리의 TemplateSpec.id 를 저장한다. 재렌더 시 동일 폼을 복원한다.
--   transcript_id FK ON DELETE CASCADE: 원본 transcript 삭제 시 파생 문서도 함께 정리한다.

CREATE TABLE IF NOT EXISTS summary_documents (
  id            UUID PRIMARY KEY,
  transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  user_id       UUID,
  template_id   TEXT NOT NULL,
  payload       JSONB NOT NULL DEFAULT '{}',
  model         TEXT,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);

-- transcript 단위로 생성 이력을 조회하는 경로 최적화
CREATE INDEX IF NOT EXISTS idx_summary_documents_transcript
  ON summary_documents(transcript_id);

-- 사용자별 최근 생성 문서 목록 조회 최적화
CREATE INDEX IF NOT EXISTS idx_summary_documents_user_created
  ON summary_documents(user_id, created_at DESC);
