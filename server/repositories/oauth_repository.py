"""OAuth 계정 DB 조회 및 생성"""
import json
from uuid import UUID, uuid4

from db.connection import DatabaseConnection


class OAuthRepository:
    # DatabaseConnection Protocol 을 주입받아 실제 DB 와 테스트용 Fake 를 모두 수용한다.
    def __init__(self, connection: DatabaseConnection) -> None:
        self._connection = connection

    async def get_user_by_oauth(self, provider: str, oauth_id: str) -> dict | None:
        """
        기능 요약: provider + oauth_id 조합으로 연동된 사용자를 조회한다.

        기능 흐름:
            1. user_oauth_accounts와 users를 JOIN하여 단건 조회
            2. 미존재 시 None 반환 — 호출자가 신규 가입 여부를 분기 처리한다

        파라미터:
            provider: OAuth 제공자 식별자 (예: "google", "kakao", "naver")
            oauth_id: 제공자가 발급한 사용자 고유 ID (예: "1234567890")
        """
        # 1. provider + oauth_id 조합으로 users 테이블과 JOIN 조회
        row = await self._connection.fetchrow(
            """
            SELECT u.id, u.email, u.nickname
            FROM users u
            INNER JOIN user_oauth_accounts oa ON u.id = oa.user_id
            WHERE oa.provider = $1 AND oa.oauth_id = $2
            """,
            provider,
            oauth_id,
        )
        return dict(row) if row else None

    async def create_user_with_oauth(
        self,
        email: str,
        nickname: str,
        provider: str,
        oauth_id: str,
        provider_data: dict,
    ) -> UUID:
        """
        기능 요약: password_hash 없는 OAuth 전용 사용자 계정과 OAuth 연동 레코드를 함께 생성한다.

        기능 흐름:
            1. users 레코드 생성 (password_hash 미포함 — nullable 허용)
            2. user_oauth_accounts 레코드 생성하여 provider 연동

        파라미터:
            email: 사용자 이메일 (예: "user@gmail.com")
            nickname: 사용자 닉네임 (예: "홍길동")
            provider: OAuth 제공자 식별자 (예: "google")
            oauth_id: 제공자가 발급한 고유 ID (예: "1234567890")
            provider_data: 제공자로부터 받은 추가 사용자 정보 (예: {"picture": "https://..."})
        """
        # 1. users 레코드 생성 (password_hash 없는 OAuth 전용 계정)
        user_id = uuid4()
        await self._connection.execute(
            "INSERT INTO users (id, nickname, email) VALUES ($1, $2, $3)",
            user_id,
            nickname,
            email,
        )

        # 2. OAuth 계정 연동 레코드 생성
        await self._connection.execute(
            """
            INSERT INTO user_oauth_accounts (id, user_id, provider, oauth_id, provider_data)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            uuid4(),
            user_id,
            provider,
            oauth_id,
            json.dumps(provider_data),
        )

        return user_id
