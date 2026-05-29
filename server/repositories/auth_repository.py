from uuid import UUID, uuid4

from db.connection import DatabaseConnection


class AuthRepository:
    # DatabaseConnection Protocol 을 주입받아 실제 DB 와 테스트용 Fake 를 모두 수용한다.
    def __init__(self, connection: DatabaseConnection) -> None:
        self._connection = connection

    async def create_user(
        self,
        nickname: str,
        email: str,
        password_hash: str,
    ) -> UUID:
       
        # 1. 신규 UUID 생성 — DB 에서 생성하지 않고 서버에서 직접 생성해 일관성 유지
        user_id = uuid4()

        # 2. users row 삽입 후 RETURNING 으로 실제 저장된 id 를 확인
        row = await self._connection.fetchrow(
            """
            INSERT INTO users (id, nickname, email, password_hash)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            user_id,
            nickname,
            email,
            password_hash,
        )
        return row["id"] if row else user_id

    async def get_user_by_email(self, email: str) -> dict | None:
        
        # 1. idx_users_email 인덱스를 활용해 이메일로 단건 조회
        row = await self._connection.fetchrow(
            """
            SELECT id, nickname, email, password_hash, created_at, updated_at
            FROM users
            WHERE email = $1
            """,
            email,
        )

        # 2. 미존재 시 None 반환 — 호출자가 존재 여부를 분기 처리한다
        if row is None:
            return None

        return dict(row)
