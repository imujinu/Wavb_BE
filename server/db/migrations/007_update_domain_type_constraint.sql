-- domain_type CHECK 제약을 새 도메인 목록으로 교체한다.
-- 변경 전: meeting, lecture
-- 변경 후: general, legal, medical, science, it, religion

-- 1. 기존 데이터 마이그레이션 (meeting/lecture → general)
UPDATE transcripts SET domain_type = 'general' WHERE domain_type IN ('meeting', 'lecture');
UPDATE chunks SET domain_type = 'general' WHERE domain_type IN ('meeting', 'lecture');

-- 2. 기존 제약 제거 후 새 제약 추가
ALTER TABLE transcripts
  DROP CONSTRAINT transcripts_domain_type_check;

ALTER TABLE transcripts
  ADD CONSTRAINT transcripts_domain_type_check
  CHECK (domain_type IN ('general', 'legal', 'medical', 'science', 'it', 'religion'));

ALTER TABLE chunks
  DROP CONSTRAINT chunks_domain_type_check;

ALTER TABLE chunks
  ADD CONSTRAINT chunks_domain_type_check
  CHECK (domain_type IN ('general', 'legal', 'medical', 'science', 'it', 'religion'));
