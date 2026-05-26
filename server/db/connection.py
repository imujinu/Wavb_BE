from collections.abc import AsyncIterator
from typing import Any, Protocol

from fastapi import HTTPException, status

from settings import get_settings


class DatabaseConnection(Protocol):
    async def execute(self, query: str, *args: Any) -> Any: ...

    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def executemany(self, query: str, args: list[tuple[Any, ...]]) -> Any: ...


class Database:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Any | None = None

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

    async def disconnect(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    @property
    def pool(self) -> Any:
        if self._pool is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database pool is not connected.",
            )
        return self._pool


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is not None:
        return _database

    settings = get_settings()
    _database = Database(settings.database_url)
    return _database


async def connect_database() -> None:
    await get_database().connect()


async def disconnect_database() -> None:
    await get_database().disconnect()


async def get_connection() -> AsyncIterator[DatabaseConnection]:
    database = get_database()
    await database.connect()
    async with database.pool.acquire() as connection:
        yield connection
