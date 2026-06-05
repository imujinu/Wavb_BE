from pathlib import Path
from uuid import uuid4

import pytest

from repositories.work_item_repository import WorkItemRepository
from schemas.work_items import ReorderItem


class FakeConnection:
    def __init__(self) -> None:
        self.fetch_results: list[list[dict]] = []
        self.fetchrow_results: list[dict | None] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        if self.fetch_results:
            return self.fetch_results.pop(0)
        return []

    async def fetchrow(self, query: str, *args):
        self.fetchrow_calls.append((query, args))
        if self.fetchrow_results:
            return self.fetchrow_results.pop(0)
        return {"id": args[0], "name": "folder", "sort_order": 0, "created_at": None, "updated_at": None}

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))

    async def executemany(self, query: str, args: list[tuple]):
        self.executemany_calls.append((query, args))


def test_work_item_migration_adds_folders_and_transcript_columns() -> None:
    migration = Path("db/migrations/010_add_folders_work_items.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS folders" in migration
    assert "user_id UUID NOT NULL REFERENCES users(id)" in migration
    assert "ADD COLUMN IF NOT EXISTS folder_id" in migration
    assert "ADD COLUMN IF NOT EXISTS sort_order" in migration
    assert "idx_transcripts_user_folder_sort" in migration


@pytest.mark.asyncio
async def test_list_root_items_queries_owner_and_container() -> None:
    connection = FakeConnection()
    repository = WorkItemRepository(connection)
    user_id = uuid4()
    folder_id = uuid4()
    transcript_id = uuid4()
    connection.fetch_results = [
        [{
            "id": folder_id,
            "name": "lectures",
            "sort_order": 0,
            "created_at": None,
            "updated_at": None,
        }],
        [{
            "id": transcript_id,
            "title": "lecture",
            "source_audio_uri": "/uploads/user/file.pdf",
            "original_filename": "lecture.pdf",
            "mime_type": "application/pdf",
            "status": "completed",
            "sort_order": 1,
            "created_at": None,
        }],
    ]

    folders = await repository.list_root_folders(user_id)
    files = await repository.list_root_files(user_id)

    assert folders[0].id == folder_id
    assert files[0].transcript_id == transcript_id
    folder_sql, folder_args = connection.fetch_calls[0]
    file_sql, file_args = connection.fetch_calls[1]
    assert "WHERE user_id = $1" in folder_sql
    assert "ORDER BY sort_order ASC" in folder_sql
    assert "WHERE user_id = $1 AND folder_id IS NULL" in file_sql
    assert "ORDER BY sort_order ASC" in file_sql
    assert folder_args == (user_id,)
    assert file_args == (user_id,)


@pytest.mark.asyncio
async def test_list_files_by_folder_filters_owner_and_folder() -> None:
    connection = FakeConnection()
    repository = WorkItemRepository(connection)
    user_id = uuid4()
    folder_id = uuid4()

    await repository.list_files_by_folder(folder_id, user_id)

    sql, args = connection.fetch_calls[0]
    assert "WHERE user_id = $1 AND folder_id = $2" in sql
    assert args == (user_id, folder_id)


@pytest.mark.asyncio
async def test_move_files_updates_only_authenticated_user_files() -> None:
    connection = FakeConnection()
    repository = WorkItemRepository(connection)
    user_id = uuid4()
    folder_id = uuid4()
    transcript_ids = [uuid4(), uuid4()]

    await repository.move_files(transcript_ids, user_id, folder_id)

    sql, args = connection.execute_calls[0]
    assert "UPDATE transcripts" in sql
    assert "WHERE user_id = $1 AND id = ANY($2::uuid[])" in sql
    assert args == (user_id, transcript_ids, folder_id)


@pytest.mark.asyncio
async def test_root_reorder_updates_folder_and_root_file_sort_orders() -> None:
    connection = FakeConnection()
    repository = WorkItemRepository(connection)
    user_id = uuid4()
    folder_id = uuid4()
    file_id = uuid4()

    await repository.update_root_sort_orders(
        folder_items=[ReorderItem(type="folder", id=folder_id, sort_order=0)],
        file_items=[ReorderItem(type="file", id=file_id, sort_order=1)],
        user_id=user_id,
    )

    folder_sql, folder_args = connection.executemany_calls[0]
    file_sql, file_args = connection.executemany_calls[1]
    assert "UPDATE folders" in folder_sql
    assert "WHERE user_id = $1 AND id = $2" in folder_sql
    assert folder_args == [(user_id, folder_id, 0)]
    assert "UPDATE transcripts" in file_sql
    assert "folder_id IS NULL" in file_sql
    assert file_args == [(user_id, file_id, 1)]


@pytest.mark.asyncio
async def test_count_methods_include_owner_filters() -> None:
    connection = FakeConnection()
    repository = WorkItemRepository(connection)
    user_id = uuid4()
    folder_id = uuid4()
    transcript_ids = [uuid4()]
    connection.fetchrow_results = [{"count": 1}, {"count": 1}, {"count": 1}]

    owned = await repository.count_owned_transcripts(transcript_ids, user_id)
    root = await repository.count_root_files(transcript_ids, user_id)
    folder = await repository.count_files_in_folder(transcript_ids, folder_id, user_id)

    assert owned == 1
    assert root == 1
    assert folder == 1
    owned_sql, owned_args = connection.fetchrow_calls[0]
    root_sql, root_args = connection.fetchrow_calls[1]
    folder_sql, folder_args = connection.fetchrow_calls[2]
    assert "WHERE user_id = $1 AND id = ANY($2::uuid[])" in owned_sql
    assert owned_args == (user_id, transcript_ids)
    assert "folder_id IS NULL" in root_sql
    assert root_args == (user_id, transcript_ids)
    assert "folder_id = $2" in folder_sql
    assert folder_args == (user_id, folder_id, transcript_ids)
