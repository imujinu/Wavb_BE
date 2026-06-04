-- Active: 1780363895240@@127.0.0.1@5432@recordoc
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS transcripts (
  id UUID PRIMARY KEY,
  user_id UUID,
  title TEXT,
  source_audio_uri TEXT NOT NULL,
  original_filename TEXT,
  mime_type TEXT,
  duration_seconds NUMERIC,
  language TEXT DEFAULT 'ko',
  stt_model TEXT,
  full_text TEXT,
  summary TEXT,
  status TEXT NOT NULL DEFAULT 'uploaded',
  error_message TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
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
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (transcript_id, chunk_strategy, chunk_index)
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

CREATE INDEX IF NOT EXISTS idx_transcripts_created_at
  ON transcripts(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_segments_transcript_index
  ON segments(transcript_id, segment_index);

CREATE INDEX IF NOT EXISTS idx_segments_speaker
  ON segments(transcript_id, speaker_label);

CREATE INDEX IF NOT EXISTS idx_segments_time
  ON segments(transcript_id, start_seconds, end_seconds);

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

CREATE INDEX IF NOT EXISTS idx_lecture_summaries_transcript
  ON lecture_summaries(transcript_id);

CREATE INDEX IF NOT EXISTS idx_lecture_summaries_user_created
  ON lecture_summaries(user_id, created_at DESC);
