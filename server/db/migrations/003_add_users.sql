-- users 테이블 생성: 이메일/비밀번호 기반 자체 인증 사용자를 영속화한다.
-- transcripts.user_id 가 이 테이블을 참조하도록 외래키 제약을 추가한다.
CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY,
  nickname      TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);

-- 이메일 기준 단건 조회가 로그인/중복 확인 경로에서 자주 발생하므로 인덱스를 추가한다.
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- transcripts.user_id → users.id 외래키 제약 추가
-- 사용자 탈퇴 시 transcript 레코드는 보존하되 user_id를 NULL로 비워 고아 레코드를 방지한다.
ALTER TABLE transcripts
  ADD CONSTRAINT fk_transcripts_user
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
