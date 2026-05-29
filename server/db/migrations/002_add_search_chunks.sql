-- search_chunks: parent chunk를 의미 단위로 분할한 검색 전용 하위 chunk 테이블
-- parent chunk(chunks)와 1:N 관계이며, embedding 기반 유사도 검색의 실제 대상 단위로 사용된다.
-- v1에서는 GIN(metadata) 인덱스를 생략하고, 검색 API 쿼리 패턴 확정 후 003 migration에서 추가한다.

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
  embedding_model TEXT NOT NULL,
  embedding vector(1536),
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (parent_chunk_id, child_index)
);

-- transcript 단위 전체 조회 및 삭제 경로 최적화
CREATE INDEX IF NOT EXISTS idx_search_chunks_transcript
  ON search_chunks(transcript_id);

-- parent chunk 기준 자식 순서 탐색 최적화
CREATE INDEX IF NOT EXISTS idx_search_chunks_parent
  ON search_chunks(parent_chunk_id, child_index);

-- cosine 유사도 기반 벡터 검색 최적화
CREATE INDEX IF NOT EXISTS idx_search_chunks_embedding
  ON search_chunks USING ivfflat (embedding vector_cosine_ops);
