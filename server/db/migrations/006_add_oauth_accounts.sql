-- OAuth 소셜 로그인을 위한 계정 연동 테이블.
-- 동일한 사용자가 여러 제공자로 로그인할 수 있도록 users와 N:1 관계.

CREATE TABLE IF NOT EXISTS user_oauth_accounts (
    id            UUID        PRIMARY KEY,
    user_id       UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT        NOT NULL,   -- 'google' | 'kakao' | 'naver'
    oauth_id      TEXT        NOT NULL,   -- 제공자가 부여한 고유 사용자 ID
    provider_data JSONB,                  -- 제공자 응답 원본 보관
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_provider_oauth_id UNIQUE (provider, oauth_id)
);

CREATE INDEX IF NOT EXISTS idx_oauth_accounts_user_id
    ON user_oauth_accounts(user_id);

-- OAuth 사용자는 비밀번호가 없으므로 NULL 허용 (기존 password 기반 계정은 영향 없음)
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
