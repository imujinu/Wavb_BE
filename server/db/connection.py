from typing import Any

from fastapi import HTTPException, status

from settings import get_settings


class Database:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Any | None = None

    # PostgreSQL pool을 지연 생성해서 DB가 없는 테스트/로컬 import 흐름을 깨지 않게 한다.
    async def connect(self) -> None:
        if self._pool is not None:
            return
        if not self._database_url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="DATABASE_URL is not configured.",
            )

        try:
            import asyncpg
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="asyncpg is required to connect to PostgreSQL.",
            ) from exc

        self._pool = await asyncpg.create_pool(self._database_url)

    # 앱 종료 시 pool을 닫아 PostgreSQL connection 누수를 막는다.
    async def disconnect(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    # repository가 transaction/acquire를 직접 사용할 수 있도록 pool을 제공한다.
    @property
    def pool(self) -> Any:
        if self._pool is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database pool is not connected.",
            )
        return self._pool


def get_database() -> Database:
    settings = get_settings()
    return Database(settings.database_url)
