-- Recordoc full schema for a fresh PostgreSQL database.
-- Keep this file in sync with server/db/migrations/*.sql.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY,
  nickname TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS folders (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sort_order INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transcripts (
  id UUID PRIMARY KEY,
  user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  folder_id UUID REFERENCES folders(id) ON DELETE SET NULL,
  title TEXT,
  source_audio_uri TEXT NOT NULL,
  original_filename TEXT,
  mime_type TEXT,
  source_type TEXT,
  duration_seconds NUMERIC,
  language TEXT DEFAULT 'ko',
  stt_model TEXT,
  full_text TEXT,
  temporary_text TEXT,
  summary TEXT,
  status TEXT NOT NULL DEFAULT 'uploaded',
  content_status TEXT NOT NULL DEFAULT 'pending',
  index_status TEXT NOT NULL DEFAULT 'pending',
  error_message TEXT,
  cancel_requested_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ,
  sort_order INT NOT NULL DEFAULT 0,
  processed_at TIMESTAMPTZ,
  indexed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_oauth_accounts (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  oauth_id TEXT NOT NULL,
  provider_data JSONB,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT uq_provider_oauth_id UNIQUE (provider, oauth_id)
);

CREATE TABLE IF NOT EXISTS segments (
  id UUID PRIMARY KEY,
  transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  segment_index INT NOT NULL,
  speaker_label TEXT,
  start_seconds NUMERIC NOT NULL,
  end_seconds NUMERIC NOT NULL,
  text TEXT NOT NULL,
  confidence NUMERIC,
  raw_metadata JSONB DEFAULT '{}',
  source_type TEXT,
  source_page_start INT,
  source_page_end INT,
  source_slide_start INT,
  source_slide_end INT,
  source_start_seconds NUMERIC,
  source_end_seconds NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (transcript_id, segment_index)
);

CREATE TABLE IF NOT EXISTS chunks (
  id UUID PRIMARY KEY,
  transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  chunk_strategy TEXT NOT NULL,
  segment_start_index INT,
  segment_end_index INT,
  start_seconds NUMERIC,
  end_seconds NUMERIC,
  text TEXT NOT NULL,
  summary TEXT,
  topic TEXT,
  subtopic TEXT,
  keywords TEXT[] DEFAULT '{}',
  speaker_labels TEXT[] DEFAULT '{}',
  metadata JSONB DEFAULT '{}',
  embedding_model TEXT,
  embedding vector(1536),
  source_type TEXT,
  source_page_start INT,
  source_page_end INT,
  source_slide_start INT,
  source_slide_end INT,
  source_start_seconds NUMERIC,
  source_end_seconds NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (transcript_id, chunk_strategy, chunk_index)
);

CREATE TABLE IF NOT EXISTS search_chunks (
  id UUID PRIMARY KEY,
  transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  parent_chunk_id UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  child_index INT NOT NULL,
  segment_start_index INT,
  segment_end_index INT,
  start_seconds NUMERIC,
  end_seconds NUMERIC,
  text TEXT NOT NULL,
  text_morphemes TEXT,
  embedding_model TEXT NOT NULL,
  embedding vector(1536),
  metadata JSONB DEFAULT '{}',
  source_type TEXT,
  source_page_start INT,
  source_page_end INT,
  source_slide_start INT,
  source_slide_end INT,
  source_start_seconds NUMERIC,
  source_end_seconds NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (parent_chunk_id, child_index)
);

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

CREATE TABLE IF NOT EXISTS summary_documents (
  id UUID PRIMARY KEY,
  transcript_id UUID NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  user_id UUID,
  template_id TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  model TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_email
  ON users(email);

CREATE INDEX IF NOT EXISTS idx_oauth_accounts_user_id
  ON user_oauth_accounts(user_id);

CREATE INDEX IF NOT EXISTS idx_folders_user_sort
  ON folders(user_id, sort_order, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_transcripts_created_at
  ON transcripts(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_transcripts_user_folder_sort
  ON transcripts(user_id, folder_id, sort_order, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_transcripts_processing_status
  ON transcripts(user_id, status, content_status, index_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_transcripts_cancel_requested
  ON transcripts(user_id, cancel_requested_at)
  WHERE cancel_requested_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_segments_transcript_index
  ON segments(transcript_id, segment_index);

CREATE INDEX IF NOT EXISTS idx_segments_speaker
  ON segments(transcript_id, speaker_label);

CREATE INDEX IF NOT EXISTS idx_segments_time
  ON segments(transcript_id, start_seconds, end_seconds);

CREATE INDEX IF NOT EXISTS idx_segments_source_range
  ON segments(
    source_type,
    source_page_start,
    source_page_end,
    source_slide_start,
    source_slide_end,
    source_start_seconds,
    source_end_seconds
  );

CREATE INDEX IF NOT EXISTS idx_chunks_transcript
  ON chunks(transcript_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_chunks_keywords
  ON chunks USING GIN (keywords);

CREATE INDEX IF NOT EXISTS idx_chunks_speakers
  ON chunks USING GIN (speaker_labels);

CREATE INDEX IF NOT EXISTS idx_chunks_metadata
  ON chunks USING GIN (metadata);

CREATE INDEX IF NOT EXISTS idx_chunks_text_fts
  ON chunks USING GIN (to_tsvector('simple', text));

CREATE INDEX IF NOT EXISTS idx_chunks_embedding
  ON chunks USING ivfflat (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_chunks_source_range
  ON chunks(
    source_type,
    source_page_start,
    source_page_end,
    source_slide_start,
    source_slide_end,
    source_start_seconds,
    source_end_seconds
  );

CREATE INDEX IF NOT EXISTS idx_search_chunks_transcript
  ON search_chunks(transcript_id);

CREATE INDEX IF NOT EXISTS idx_search_chunks_parent
  ON search_chunks(parent_chunk_id, child_index);

CREATE INDEX IF NOT EXISTS idx_search_chunks_embedding
  ON search_chunks USING ivfflat (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_search_chunks_morphemes_fts
  ON search_chunks
  USING GIN (to_tsvector('simple', coalesce(text_morphemes, text)));

CREATE INDEX IF NOT EXISTS idx_search_chunks_source_range
  ON search_chunks(
    source_type,
    source_page_start,
    source_page_end,
    source_slide_start,
    source_slide_end,
    source_start_seconds,
    source_end_seconds
  );

CREATE INDEX IF NOT EXISTS idx_temporary_segments_transcript_index
  ON temporary_segments(transcript_id, segment_index);

CREATE INDEX IF NOT EXISTS idx_lecture_summaries_transcript
  ON lecture_summaries(transcript_id);

CREATE INDEX IF NOT EXISTS idx_lecture_summaries_user_created
  ON lecture_summaries(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_summary_documents_transcript
  ON summary_documents(transcript_id);

CREATE INDEX IF NOT EXISTS idx_summary_documents_user_created
  ON summary_documents(user_id, created_at DESC);
