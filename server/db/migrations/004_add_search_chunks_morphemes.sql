-- Active: 1780321728388@@127.0.0.1@5432@recordoc
-- search_chunks 테이블에 형태소 분석 결과를 저장하는 text_morphemes 컬럼 추가
-- 원본 text를 보존하면서 FTS 전용 형태소 텍스트를 분리 관리한다.
--
-- 설계 이유:
--   text_morphemes 별도 컬럼: 원본 텍스트(text)를 보존하면서 FTS 전용 형태소 텍스트를 분리 관리
--   coalesce(text_morphemes, text): text_morphemes가 NULL인 기존 rows도 FTS 인덱스에 포함되도록 fallback
--   'simple' dictionary: 형태소 분석으로 이미 어간이 분리된 상태이므로 PostgreSQL 추가 stemming 불필요

ALTER TABLE search_chunks
  ADD COLUMN IF NOT EXISTS text_morphemes TEXT;

-- FTS 검색 성능 최적화를 위한 GIN 인덱스 추가
-- coalesce(text_morphemes, text)로 NULL인 기존 rows도 원문 기반 FTS 인덱스에 포함
CREATE INDEX IF NOT EXISTS idx_search_chunks_morphemes_fts
  ON search_chunks
  USING GIN (to_tsvector('simple', coalesce(text_morphemes, text)));
