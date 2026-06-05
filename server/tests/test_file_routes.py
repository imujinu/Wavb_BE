from uuid import UUID
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from dependencies.auth import get_current_user
from main import app
from routes import files
from schemas.auth import CurrentUser
from schemas.rag import UploadedFileDetail
from services.files.file_ingestion_service import FileIngestionResult


client = TestClient(app)


def test_file_upload_route_uses_authenticated_user() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("11111111-1111-1111-1111-111111111111")

    class FakeFileIngestionService:
        async def ingest_upload(self, file, file_name, user_id, folder_id=None):
            assert file.filename == "lecture.pdf"
            assert file_name == "lecture.pdf"
            assert user_id == fake_user_id
            assert folder_id is None
            return FileIngestionResult(
                transcript_id=transcript_id,
                source_type="document",
                file_uri="/uploads/test/lecture.pdf",
                transcript="강의 자료 텍스트",
                segment_count=2,
                chunk_count=1,
                status="completed",
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_file_ingestion_service] = (
        lambda: FakeFileIngestionService()
    )

    try:
        response = client.post(
            "/files/upload",
            data={
                "file_name": "lecture.pdf",
            },
            files={"file": ("lecture.pdf", b"fake pdf", "application/pdf")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "transcript_id": str(transcript_id),
        "source_type": "document",
        "file_uri": "/uploads/test/lecture.pdf",
        "transcript": "강의 자료 텍스트",
        "segment_count": 2,
        "chunk_count": 1,
        "status": "completed",
    }


def test_file_upload_allows_missing_optional_metadata() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("22222222-2222-2222-2222-222222222222")

    class FakeFileIngestionService:
        async def ingest_upload(self, file, file_name, user_id, folder_id=None):
            assert file.filename == "meeting.mp3"
            assert file_name is None
            assert user_id == fake_user_id
            assert folder_id is None
            return FileIngestionResult(
                transcript_id=transcript_id,
                source_type="audio",
                file_uri="/uploads/test/meeting.mp3",
                transcript="회의 전사",
                segment_count=1,
                chunk_count=1,
                status="completed",
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_file_ingestion_service] = (
        lambda: FakeFileIngestionService()
    )

    try:
        response = client.post(
            "/files/upload",
            files={"file": ("meeting.mp3", b"fake audio", "audio/mpeg")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["source_type"] == "audio"
    assert response.json()["transcript_id"] == str(transcript_id)


def test_file_upload_route_accepts_folder_id() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    folder_id = UUID("44444444-4444-4444-4444-444444444444")
    transcript_id = UUID("55555555-5555-5555-5555-555555555555")

    class FakeFileIngestionService:
        async def ingest_upload(self, file, file_name, user_id, folder_id=None):
            assert file.filename == "lecture.pdf"
            assert file_name == "lecture.pdf"
            assert user_id == fake_user_id
            assert folder_id == UUID("44444444-4444-4444-4444-444444444444")
            return FileIngestionResult(
                transcript_id=transcript_id,
                source_type="document",
                file_uri="/uploads/test/lecture.pdf",
                transcript="강의 자료 텍스트",
                segment_count=2,
                chunk_count=1,
                status="completed",
                folder_id=folder_id,
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_file_ingestion_service] = (
        lambda: FakeFileIngestionService()
    )

    try:
        response = client.post(
            "/files/upload",
            data={
                "file_name": "lecture.pdf",
                "folder_id": str(folder_id),
            },
            files={"file": ("lecture.pdf", b"fake pdf", "application/pdf")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["folder_id"] == str(folder_id)


def test_list_uploaded_files_returns_authenticated_user_files() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("33333333-3333-3333-3333-333333333333")
    created_at = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    class FakeRepository:
        async def list_transcripts_by_user(self, user_id):
            assert user_id == fake_user_id
            return [
                UploadedFileDetail(
                    transcript_id=transcript_id,
                    title="lecture",
                    file_uri="/uploads/user-id/uuid.pdf",
                    original_filename="lecture.pdf",
                    mime_type="application/pdf",
                    status="completed",
                    created_at=created_at,
                )
            ]

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_rag_repository] = lambda: FakeRepository()

    try:
        response = client.get("/files")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "transcript_id": str(transcript_id),
            "title": "lecture",
            "file_uri": "/uploads/user-id/uuid.pdf",
            "original_filename": "lecture.pdf",
            "mime_type": "application/pdf",
            "status": "completed",
            "created_at": "2026-06-05T12:00:00+00:00",
        }
    ]
