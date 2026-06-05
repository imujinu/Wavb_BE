-- Add nullable source range columns for future scoped retrieval.
-- PDF rows use page columns, PPT/PPTX rows use slide columns, and audio rows use seconds columns.

ALTER TABLE segments
  ADD COLUMN IF NOT EXISTS source_type TEXT,
  ADD COLUMN IF NOT EXISTS source_page_start INT,
  ADD COLUMN IF NOT EXISTS source_page_end INT,
  ADD COLUMN IF NOT EXISTS source_slide_start INT,
  ADD COLUMN IF NOT EXISTS source_slide_end INT,
  ADD COLUMN IF NOT EXISTS source_start_seconds NUMERIC,
  ADD COLUMN IF NOT EXISTS source_end_seconds NUMERIC;

ALTER TABLE chunks
  ADD COLUMN IF NOT EXISTS source_type TEXT,
  ADD COLUMN IF NOT EXISTS source_page_start INT,
  ADD COLUMN IF NOT EXISTS source_page_end INT,
  ADD COLUMN IF NOT EXISTS source_slide_start INT,
  ADD COLUMN IF NOT EXISTS source_slide_end INT,
  ADD COLUMN IF NOT EXISTS source_start_seconds NUMERIC,
  ADD COLUMN IF NOT EXISTS source_end_seconds NUMERIC;

ALTER TABLE search_chunks
  ADD COLUMN IF NOT EXISTS source_type TEXT,
  ADD COLUMN IF NOT EXISTS source_page_start INT,
  ADD COLUMN IF NOT EXISTS source_page_end INT,
  ADD COLUMN IF NOT EXISTS source_slide_start INT,
  ADD COLUMN IF NOT EXISTS source_slide_end INT,
  ADD COLUMN IF NOT EXISTS source_start_seconds NUMERIC,
  ADD COLUMN IF NOT EXISTS source_end_seconds NUMERIC;

CREATE INDEX IF NOT EXISTS idx_segments_source_range
  ON segments(source_type, source_page_start, source_page_end,
              source_slide_start, source_slide_end,
              source_start_seconds, source_end_seconds);

CREATE INDEX IF NOT EXISTS idx_chunks_source_range
  ON chunks(source_type, source_page_start, source_page_end,
            source_slide_start, source_slide_end,
            source_start_seconds, source_end_seconds);

CREATE INDEX IF NOT EXISTS idx_search_chunks_source_range
  ON search_chunks(source_type, source_page_start, source_page_end,
                   source_slide_start, source_slide_end,
                   source_start_seconds, source_end_seconds);
