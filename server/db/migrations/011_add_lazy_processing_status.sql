ALTER TABLE transcripts
  ADD COLUMN IF NOT EXISTS source_type TEXT,
  ADD COLUMN IF NOT EXISTS content_status TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS index_status TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS temporary_text TEXT,
  ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ;

UPDATE transcripts
SET content_status = 'completed',
    index_status = 'completed',
    processed_at = COALESCE(processed_at, updated_at),
    indexed_at = COALESCE(indexed_at, updated_at)
WHERE status = 'completed'
  AND content_status = 'pending'
  AND index_status = 'pending';

CREATE TABLE IF NOT EXISTS temporary_segments (
  id UUID PRIMARY KEY,
  transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  segment_index INT NOT NULL,
  start_seconds NUMERIC,
  end_seconds NUMERIC,
  text TEXT NOT NULL,
  raw_metadata JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (transcript_id, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_temporary_segments_transcript_index
  ON temporary_segments(transcript_id, segment_index);

CREATE INDEX IF NOT EXISTS idx_transcripts_processing_status
  ON transcripts(user_id, status, content_status, index_status, created_at DESC);
