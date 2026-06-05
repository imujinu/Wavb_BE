from typing import Any
from uuid import UUID, uuid4

from db.connection import DatabaseConnection
from schemas.work_items import FileWorkItemRecord, FolderRecord, ReorderItem


class _NoopTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class WorkItemRepository:
    def __init__(self, connection: DatabaseConnection) -> None:
        self._connection = connection

    # 기능 요약: 폴더를 생성하고 생성된 폴더 row를 반환한다.
    # 기능 흐름: 현재 사용자의 폴더 마지막 sort_order 다음 값을 계산한 뒤 INSERT RETURNING으로 생성 결과를 받는다.
    # 파라미터: user_id는 인증 사용자 UUID, name은 저장할 폴더명.
    async def create_folder(self, user_id: UUID, name: str) -> FolderRecord:
        folder_id = uuid4()
        row = await self._connection.fetchrow(
            """
            INSERT INTO folders (id, user_id, name, sort_order)
            VALUES (
              $1,
              $2,
              $3,
              COALESCE((SELECT MAX(sort_order) + 1 FROM folders WHERE user_id = $2), 0)
            )
            RETURNING id, name, sort_order, created_at, updated_at
            """,
            folder_id,
            user_id,
            name,
        )
        return self._to_folder_record(row)

    # 기능 요약: 인증 사용자가 소유한 폴더명을 변경한다.
    # 기능 흐름: id와 user_id를 함께 조건으로 걸어 UPDATE RETURNING을 수행하고 없으면 None을 반환한다.
    # 파라미터: folder_id는 대상 폴더 UUID, user_id는 인증 사용자 UUID, name은 새 폴더명.
    async def update_folder_name(
        self,
        folder_id: UUID,
        user_id: UUID,
        name: str,
    ) -> FolderRecord | None:
        row = await self._connection.fetchrow(
            """
            UPDATE folders
            SET name = $3, updated_at = now()
            WHERE id = $1 AND user_id = $2
            RETURNING id, name, sort_order, created_at, updated_at
            """,
            folder_id,
            user_id,
            name,
        )
        return self._to_folder_record(row) if row else None

    # 기능 요약: 폴더를 삭제하고 내부 파일은 루트로 돌려놓는다.
    # 기능 흐름: 동일 트랜잭션에서 내부 transcript.folder_id를 NULL로 바꾼 뒤 폴더를 DELETE 한다.
    # 파라미터: folder_id는 삭제할 폴더 UUID, user_id는 인증 사용자 UUID.
    async def delete_folder(self, folder_id: UUID, user_id: UUID) -> bool:
        async with self._transaction():
            folder = await self.get_folder_by_id(folder_id, user_id)
            if folder is None:
                return False

            await self.move_folder_files_to_root(folder_id, user_id)
            row = await self._connection.fetchrow(
                """
                DELETE FROM folders
                WHERE id = $1 AND user_id = $2
                RETURNING id
                """,
                folder_id,
                user_id,
            )
            return row is not None

    async def get_folder_by_id(
        self,
        folder_id: UUID,
        user_id: UUID,
    ) -> FolderRecord | None:
        row = await self._connection.fetchrow(
            """
            SELECT id, name, sort_order, created_at, updated_at
            FROM folders
            WHERE id = $1 AND user_id = $2
            """,
            folder_id,
            user_id,
        )
        return self._to_folder_record(row) if row else None

    async def list_root_folders(self, user_id: UUID) -> list[FolderRecord]:
        rows = await self._connection.fetch(
            """
            SELECT id, name, sort_order, created_at, updated_at
            FROM folders
            WHERE user_id = $1
            ORDER BY sort_order ASC, created_at DESC
            """,
            user_id,
        )
        return [self._to_folder_record(row) for row in rows]

    async def list_root_files(self, user_id: UUID) -> list[FileWorkItemRecord]:
        rows = await self._connection.fetch(
            """
            SELECT id, title, source_audio_uri, original_filename,
                   mime_type, status, sort_order, created_at
            FROM transcripts
            WHERE user_id = $1 AND folder_id IS NULL
            ORDER BY sort_order ASC, created_at DESC
            """,
            user_id,
        )
        return [self._to_file_record(row) for row in rows]

    async def list_files_by_folder(
        self,
        folder_id: UUID,
        user_id: UUID,
    ) -> list[FileWorkItemRecord]:
        rows = await self._connection.fetch(
            """
            SELECT id, title, source_audio_uri, original_filename,
                   mime_type, status, sort_order, created_at
            FROM transcripts
            WHERE user_id = $1 AND folder_id = $2
            ORDER BY sort_order ASC, created_at DESC
            """,
            user_id,
            folder_id,
        )
        return [self._to_file_record(row) for row in rows]

    # 기능 요약: 여러 파일을 특정 폴더 또는 루트로 이동한다.
    # 기능 흐름: transcripts.user_id 조건으로 대상 파일만 UPDATE하고 folder_id를 요청값으로 바꾼다.
    # 파라미터: transcript_ids는 이동할 transcript UUID 목록, user_id는 인증 사용자 UUID, folder_id는 대상 폴더 또는 None.
    async def move_files(
        self,
        transcript_ids: list[UUID],
        user_id: UUID,
        folder_id: UUID | None,
    ) -> None:
        await self._connection.execute(
            """
            UPDATE transcripts
            SET folder_id = $3, updated_at = now()
            WHERE user_id = $1 AND id = ANY($2::uuid[])
            """,
            user_id,
            transcript_ids,
            folder_id,
        )

    async def move_folder_files_to_root(self, folder_id: UUID, user_id: UUID) -> None:
        await self._connection.execute(
            """
            UPDATE transcripts
            SET folder_id = NULL, updated_at = now()
            WHERE folder_id = $1 AND user_id = $2
            """,
            folder_id,
            user_id,
        )

    async def count_owned_transcripts(
        self,
        transcript_ids: list[UUID],
        user_id: UUID,
    ) -> int:
        row = await self._connection.fetchrow(
            """
            SELECT COUNT(*) AS count
            FROM transcripts
            WHERE user_id = $1 AND id = ANY($2::uuid[])
            """,
            user_id,
            transcript_ids,
        )
        return int(row["count"]) if row else 0

    async def count_root_files(self, transcript_ids: list[UUID], user_id: UUID) -> int:
        row = await self._connection.fetchrow(
            """
            SELECT COUNT(*) AS count
            FROM transcripts
            WHERE user_id = $1 AND folder_id IS NULL AND id = ANY($2::uuid[])
            """,
            user_id,
            transcript_ids,
        )
        return int(row["count"]) if row else 0

    async def count_files_in_folder(
        self,
        transcript_ids: list[UUID],
        folder_id: UUID,
        user_id: UUID,
    ) -> int:
        row = await self._connection.fetchrow(
            """
            SELECT COUNT(*) AS count
            FROM transcripts
            WHERE user_id = $1 AND folder_id = $2 AND id = ANY($3::uuid[])
            """,
            user_id,
            folder_id,
            transcript_ids,
        )
        return int(row["count"]) if row else 0

    async def count_folders(self, folder_ids: list[UUID], user_id: UUID) -> int:
        row = await self._connection.fetchrow(
            """
            SELECT COUNT(*) AS count
            FROM folders
            WHERE user_id = $1 AND id = ANY($2::uuid[])
            """,
            user_id,
            folder_ids,
        )
        return int(row["count"]) if row else 0

    async def create_folder_from_root_files(
        self,
        user_id: UUID,
        name: str,
        transcript_ids: list[UUID],
    ) -> FolderRecord:
        async with self._transaction():
            folder = await self.create_folder(user_id, name)
            await self.move_files(transcript_ids, user_id, folder.id)
            return folder

    async def update_folder_sort_orders(
        self,
        items: list[ReorderItem],
        user_id: UUID,
    ) -> None:
        if not items:
            return
        await self._connection.executemany(
            """
            UPDATE folders
            SET sort_order = $3, updated_at = now()
            WHERE user_id = $1 AND id = $2
            """,
            [(user_id, item.id, item.sort_order) for item in items],
        )

    async def update_root_file_sort_orders(
        self,
        items: list[ReorderItem],
        user_id: UUID,
    ) -> None:
        if not items:
            return
        await self._connection.executemany(
            """
            UPDATE transcripts
            SET sort_order = $3, updated_at = now()
            WHERE user_id = $1 AND id = $2 AND folder_id IS NULL
            """,
            [(user_id, item.id, item.sort_order) for item in items],
        )

    async def update_folder_file_sort_orders(
        self,
        items: list[ReorderItem],
        folder_id: UUID,
        user_id: UUID,
    ) -> None:
        if not items:
            return
        await self._connection.executemany(
            """
            UPDATE transcripts
            SET sort_order = $4, updated_at = now()
            WHERE user_id = $1 AND folder_id = $2 AND id = $3
            """,
            [(user_id, folder_id, item.id, item.sort_order) for item in items],
        )

    async def update_root_sort_orders(
        self,
        folder_items: list[ReorderItem],
        file_items: list[ReorderItem],
        user_id: UUID,
    ) -> None:
        async with self._transaction():
            await self.update_folder_sort_orders(folder_items, user_id)
            await self.update_root_file_sort_orders(file_items, user_id)

    def _transaction(self) -> Any:
        transaction = getattr(self._connection, "transaction", None)
        return transaction() if transaction else _NoopTransaction()

    def _to_folder_record(self, row: Any) -> FolderRecord:
        return FolderRecord(
            id=row["id"],
            name=row["name"],
            sort_order=row["sort_order"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _to_file_record(self, row: Any) -> FileWorkItemRecord:
        return FileWorkItemRecord(
            transcript_id=row["id"],
            title=row["title"],
            file_uri=row["source_audio_uri"],
            original_filename=row["original_filename"],
            mime_type=row["mime_type"],
            status=row["status"],
            sort_order=row["sort_order"],
            created_at=row["created_at"],
        )
