from uuid import UUID
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from dependencies.auth import get_current_user
from main import app
from routes import files
from schemas.auth import CurrentUser
from schemas.rag import (
    FileDetail,
    LectureSummaryDetail,
    SegmentCreate,
    TranscriptDetail,
    UploadedFileDetail,
)
from services.files.file_ingestion_service import FileIngestionResult
from services.files.transcript_processing_service import TranscriptProcessingResult


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
                source_type="pdf",
                file_uri="/uploads/test/lecture.pdf",
                transcript="",
                segment_count=0,
                chunk_count=0,
                status="uploaded",
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
        "source_type": "pdf",
        "file_uri": "/uploads/test/lecture.pdf",
        "transcript": "",
        "segment_count": 0,
        "chunk_count": 0,
        "status": "uploaded",
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
                transcript="",
                segment_count=0,
                chunk_count=0,
                status="uploaded",
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
                source_type="pdf",
                file_uri="/uploads/test/lecture.pdf",
                transcript="",
                segment_count=0,
                chunk_count=0,
                status="uploaded",
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


def test_process_file_route_uses_authenticated_user() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("66666666-6666-6666-6666-666666666666")

    class FakeProcessingService:
        async def process(self, transcript_id: UUID, user_id: UUID):
            assert transcript_id == UUID("66666666-6666-6666-6666-666666666666")
            assert user_id == fake_user_id
            return TranscriptProcessingResult(
                transcript_id=transcript_id,
                status="completed",
                content_status="completed",
                index_status="completed",
                segment_count=3,
                chunk_count=1,
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_transcript_processing_service] = (
        lambda: FakeProcessingService()
    )

    try:
        response = client.post(f"/files/{transcript_id}/process")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "transcript_id": str(transcript_id),
        "status": "completed",
        "content_status": "completed",
        "index_status": "completed",
        "segment_count": 3,
        "chunk_count": 1,
    }


def test_process_file_content_route_uses_authenticated_user() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("12121212-1212-1212-1212-121212121212")

    class FakeProcessingService:
        async def process_content(self, transcript_id: UUID, user_id: UUID):
            assert transcript_id == UUID("12121212-1212-1212-1212-121212121212")
            assert user_id == fake_user_id
            return TranscriptProcessingResult(
                transcript_id=transcript_id,
                status="processing",
                content_status="completed",
                index_status="pending",
                segment_count=3,
                chunk_count=0,
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_transcript_processing_service] = (
        lambda: FakeProcessingService()
    )

    try:
        response = client.post(f"/files/{transcript_id}/content")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "transcript_id": str(transcript_id),
        "status": "processing",
        "content_status": "completed",
        "index_status": "pending",
        "segment_count": 3,
        "chunk_count": 0,
    }


def test_process_file_index_route_uses_authenticated_user() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("13131313-1313-1313-1313-131313131313")

    class FakeProcessingService:
        async def process_index(self, transcript_id: UUID, user_id: UUID):
            assert transcript_id == UUID("13131313-1313-1313-1313-131313131313")
            assert user_id == fake_user_id
            return TranscriptProcessingResult(
                transcript_id=transcript_id,
                status="completed",
                content_status="completed",
                index_status="completed",
                segment_count=3,
                chunk_count=1,
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_transcript_processing_service] = (
        lambda: FakeProcessingService()
    )

    try:
        response = client.post(f"/files/{transcript_id}/index")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "transcript_id": str(transcript_id),
        "status": "completed",
        "content_status": "completed",
        "index_status": "completed",
        "segment_count": 3,
        "chunk_count": 1,
    }


def test_cancel_file_processing_route_uses_authenticated_user() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("77777777-7777-7777-7777-777777777777")

    class FakeProcessingService:
        async def cancel(self, transcript_id: UUID, user_id: UUID):
            assert transcript_id == UUID("77777777-7777-7777-7777-777777777777")
            assert user_id == fake_user_id
            return TranscriptProcessingResult(
                transcript_id=transcript_id,
                status="cancelled",
                content_status="cancelled",
                index_status="cancelled",
                segment_count=0,
                chunk_count=0,
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_transcript_processing_service] = (
        lambda: FakeProcessingService()
    )

    try:
        response = client.post(f"/files/{transcript_id}/cancel")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "transcript_id": str(transcript_id),
        "status": "cancelled",
        "content_status": "cancelled",
        "index_status": "cancelled",
        "segment_count": 0,
        "chunk_count": 0,
    }


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
                    content_status="completed",
                    index_status="completed",
                    error_message=None,
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
            "content_status": "completed",
            "index_status": "completed",
            "error_message": None,
            "created_at": "2026-06-05T12:00:00+00:00",
        }
    ]


def test_get_file_detail_returns_nested_summary() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("88888888-8888-8888-8888-888888888888")
    summary_id = UUID("99999999-9999-9999-9999-999999999999")
    created_at = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    updated_at = datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc)

    class FakeRepository:
        async def get_file_detail_by_id(self, received_transcript_id, user_id):
            assert received_transcript_id == transcript_id
            assert user_id == fake_user_id
            return FileDetail(
                transcript_id=transcript_id,
                title="lecture",
                file_uri="/uploads/user-id/lecture.pdf",
                original_filename="lecture.pdf",
                mime_type="application/pdf",
                source_type="pdf",
                status="completed",
                content_status="completed",
                index_status="completed",
                error_message=None,
                duration_seconds=None,
                created_at=created_at,
                updated_at=updated_at,
            )

        async def count_segments_by_transcript(self, received_transcript_id):
            assert received_transcript_id == transcript_id
            return 29

        async def count_chunks_by_transcript(self, received_transcript_id):
            assert received_transcript_id == transcript_id
            return 3

        async def get_lecture_summary_by_transcript(self, received_transcript_id, user_id):
            assert received_transcript_id == transcript_id
            assert user_id == fake_user_id
            return LectureSummaryDetail(
                id=summary_id,
                transcript_id=transcript_id,
                user_id=fake_user_id,
                payload={
                    "overview": {
                        "title": "lecture",
                        "summary": "summary text",
                        "key_points": ["point"],
                    },
                    "contexts": [],
                    "keywords": [],
                },
                model="gpt-4o-mini",
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_rag_repository] = lambda: FakeRepository()

    try:
        response = client.get(f"/files/{transcript_id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["transcript_id"] == str(transcript_id)
    assert body["segment_count"] == 29
    assert body["chunk_count"] == 3
    assert body["summary"]["summary_id"] == str(summary_id)
    assert body["summary"]["transcript_id"] == str(transcript_id)
    assert body["summary"]["persona_id"] == "general"
    assert body["summary"]["overview"]["summary"] == "summary text"
    assert "full_text" not in body
    assert "segments" not in body
    assert body["created_at"] == "2026-06-05T12:00:00+00:00"
    assert body["updated_at"] == "2026-06-05T12:30:00+00:00"


def test_get_file_detail_returns_null_summary_when_missing() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    class FakeRepository:
        async def get_file_detail_by_id(self, received_transcript_id, user_id):
            assert received_transcript_id == transcript_id
            assert user_id == fake_user_id
            return FileDetail(
                transcript_id=transcript_id,
                title="lecture",
                file_uri="/uploads/user-id/lecture.pdf",
                original_filename="lecture.pdf",
                mime_type="application/pdf",
                source_type="pdf",
                status="completed",
                content_status="completed",
                index_status="completed",
            )

        async def count_segments_by_transcript(self, received_transcript_id):
            return 1

        async def count_chunks_by_transcript(self, received_transcript_id):
            return 0

        async def get_lecture_summary_by_transcript(self, received_transcript_id, user_id):
            return None

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_rag_repository] = lambda: FakeRepository()

    try:
        response = client.get(f"/files/{transcript_id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["summary"] is None


def test_get_file_detail_returns_404_when_missing() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    class FakeRepository:
        async def get_file_detail_by_id(self, received_transcript_id, user_id):
            assert received_transcript_id == transcript_id
            assert user_id == fake_user_id
            return None

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_rag_repository] = lambda: FakeRepository()

    try:
        response = client.get(f"/files/{transcript_id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "Transcript not found."


def test_get_file_transcript_returns_full_text_and_segments_without_raw_metadata() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

    class FakeRepository:
        async def get_transcript_by_id(self, received_transcript_id, user_id):
            assert received_transcript_id == transcript_id
            assert user_id == fake_user_id
            return TranscriptDetail(
                id=transcript_id,
                user_id=fake_user_id,
                title="lecture",
                full_text="first second",
                summary=None,
                duration_seconds=4.2,
                language="ko",
                status="completed",
                created_at=None,
            )

        async def fetch_segments_by_transcript(self, received_transcript_id):
            assert received_transcript_id == transcript_id
            return [
                SegmentCreate(
                    segment_index=0,
                    start_seconds=0.0,
                    end_seconds=4.2,
                    speaker_label="speaker_1",
                    text="first",
                    confidence=0.98,
                    raw_metadata={"provider": "internal"},
                    source_type="audio",
                    source_start_seconds=0.0,
                    source_end_seconds=4.2,
                )
            ]

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_rag_repository] = lambda: FakeRepository()

    try:
        response = client.get(f"/files/{transcript_id}/transcript")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["transcript_id"] == str(transcript_id)
    assert body["full_text"] == "first second"
    assert body["segments"] == [
        {
            "segment_index": 0,
            "start_seconds": 0.0,
            "end_seconds": 4.2,
            "speaker_label": "speaker_1",
            "text": "first",
            "confidence": 0.98,
            "source_type": "audio",
            "source_page_start": None,
            "source_page_end": None,
            "source_slide_start": None,
            "source_slide_end": None,
            "source_start_seconds": 0.0,
            "source_end_seconds": 4.2,
        }
    ]
    assert "raw_metadata" not in body["segments"][0]


def test_get_file_transcript_returns_empty_segments() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

    class FakeRepository:
        async def get_transcript_by_id(self, received_transcript_id, user_id):
            return TranscriptDetail(
                id=transcript_id,
                user_id=fake_user_id,
                title="lecture",
                full_text=None,
                summary=None,
                duration_seconds=None,
                language=None,
                status="uploaded",
                created_at=None,
            )

        async def fetch_segments_by_transcript(self, received_transcript_id):
            assert received_transcript_id == transcript_id
            return []

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_rag_repository] = lambda: FakeRepository()

    try:
        response = client.get(f"/files/{transcript_id}/transcript")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["full_text"] is None
    assert response.json()["segments"] == []


def test_get_file_transcript_returns_404_when_missing() -> None:
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

    class FakeRepository:
        async def get_transcript_by_id(self, received_transcript_id, user_id):
            assert received_transcript_id == transcript_id
            assert user_id == fake_user_id
            return None

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[files.get_rag_repository] = lambda: FakeRepository()

    try:
        response = client.get(f"/files/{transcript_id}/transcript")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "Transcript not found."
