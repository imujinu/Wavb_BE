ALTER TABLE transcripts
  ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_transcripts_cancel_requested
  ON transcripts(user_id, cancel_requested_at)
  WHERE cancel_requested_at IS NOT NULL;
