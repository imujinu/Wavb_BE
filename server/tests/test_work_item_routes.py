from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient

from dependencies.auth import get_current_user
from main import app
from routes import work_items
from schemas.auth import CurrentUser
from schemas.work_items import FileWorkItemResponse, FolderWorkItemResponse


client = TestClient(app)


def test_list_work_items_returns_authenticated_user_items() -> None:
    user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    folder_id = UUID("11111111-1111-1111-1111-111111111111")
    transcript_id = UUID("22222222-2222-2222-2222-222222222222")
    created_at = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    class FakeService:
        async def list_root_items(self, current_user_id):
            assert current_user_id == user_id
            return [
                FolderWorkItemResponse(
                    id=folder_id,
                    name="lectures",
                    sort_order=0,
                    created_at=created_at,
                    updated_at=created_at,
                ),
                FileWorkItemResponse(
                    transcript_id=transcript_id,
                    title="lecture",
                    file_uri="/uploads/user/file.pdf",
                    original_filename="lecture.pdf",
                    mime_type="application/pdf",
                    status="completed",
                    sort_order=1,
                    created_at=created_at,
                ),
            ]

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[work_items.get_work_item_service] = lambda: FakeService()

    try:
        response = client.get("/work-items")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(folder_id),
            "name": "lectures",
            "sort_order": 0,
            "created_at": "2026-06-05T12:00:00Z",
            "updated_at": "2026-06-05T12:00:00Z",
            "type": "folder",
        },
        {
            "transcript_id": str(transcript_id),
            "title": "lecture",
            "file_uri": "/uploads/user/file.pdf",
            "original_filename": "lecture.pdf",
            "mime_type": "application/pdf",
            "status": "completed",
            "sort_order": 1,
            "created_at": "2026-06-05T12:00:00Z",
            "type": "file",
        },
    ]


def test_move_files_route_passes_request_to_service() -> None:
    user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("33333333-3333-3333-3333-333333333333")
    folder_id = UUID("44444444-4444-4444-4444-444444444444")

    class FakeService:
        async def move_files(self, transcript_ids, user_id: UUID, folder_id):
            assert transcript_ids == [transcript_id]
            assert user_id == UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            assert folder_id == UUID("44444444-4444-4444-4444-444444444444")

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[work_items.get_work_item_service] = lambda: FakeService()

    try:
        response = client.patch(
            "/files/folder",
            json={
                "transcript_ids": [str(transcript_id)],
                "folder_id": str(folder_id),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 204
    assert response.content == b""


def test_create_folder_from_files_route_returns_folder() -> None:
    user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    folder_id = UUID("55555555-5555-5555-5555-555555555555")
    transcript_id = UUID("66666666-6666-6666-6666-666666666666")

    class FakeService:
        async def create_folder_from_files(self, user_id: UUID, name: str, transcript_ids):
            assert user_id == UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            assert name == "new folder"
            assert transcript_ids == [transcript_id]
            return FolderWorkItemResponse(
                id=folder_id,
                name=name,
                sort_order=0,
                created_at=None,
                updated_at=None,
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[work_items.get_work_item_service] = lambda: FakeService()

    try:
        response = client.post(
            "/folders/from-files",
            json={"name": "new folder", "transcript_ids": [str(transcript_id)]},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["id"] == str(folder_id)
    assert response.json()["type"] == "folder"


def test_reorder_work_items_route_passes_items_to_service() -> None:
    user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    folder_id = UUID("77777777-7777-7777-7777-777777777777")
    file_id = UUID("88888888-8888-8888-8888-888888888888")

    class FakeService:
        async def reorder_items(self, user_id: UUID, container: str, folder_id, items):
            assert user_id == UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            assert container == "folder"
            assert folder_id == UUID("77777777-7777-7777-7777-777777777777")
            assert items[0].type == "file"
            assert items[0].id == file_id
            assert items[0].sort_order == 0

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[work_items.get_work_item_service] = lambda: FakeService()

    try:
        response = client.patch(
            "/work-items/reorder",
            json={
                "container": "folder",
                "folder_id": str(folder_id),
                "items": [{"type": "file", "id": str(file_id), "sort_order": 0}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 204
