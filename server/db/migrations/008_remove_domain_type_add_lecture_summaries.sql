-- domain_type 분기를 제거하고 강의 요약 데이터 저장소를 추가한다.

DROP INDEX IF EXISTS idx_transcripts_user_domain;
DROP INDEX IF EXISTS idx_chunks_domain_topic;

ALTER TABLE transcripts
  DROP CONSTRAINT IF EXISTS transcripts_domain_type_check;

ALTER TABLE chunks
  DROP CONSTRAINT IF EXISTS chunks_domain_type_check;

ALTER TABLE transcripts
  DROP COLUMN IF EXISTS domain_type;

ALTER TABLE chunks
  DROP COLUMN IF EXISTS domain_type;

CREATE TABLE IF NOT EXISTS lecture_summaries (
  id UUID PRIMARY KEY,
  transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  user_id UUID,
  payload JSONB NOT NULL,
  model TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (transcript_id)
);

CREATE INDEX IF NOT EXISTS idx_lecture_summaries_transcript
  ON lecture_summaries(transcript_id);

CREATE INDEX IF NOT EXISTS idx_lecture_summaries_user_created
  ON lecture_summaries(user_id, created_at DESC);
