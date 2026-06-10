import json
from uuid import UUID

from fastapi.testclient import TestClient

from dependencies.auth import get_current_user
from main import app
from routes import realtime
from schemas.auth import CurrentUser
from schemas.rag import TemporarySegmentDetail
from services.files.upload_storage_service import StoredUpload


client = TestClient(app)


class FakeStorageService:
    def __init__(self) -> None:
        self.saved = []

    async def save_upload(self, file, file_name, user_id):
        self.saved.append((file.filename, file_name, file.content_type, user_id))
        return StoredUpload(
            uri=f"/uploads/{user_id}/recording.wav",
            path=None,
            original_filename=file_name,
        )


class FakeRealtimeRepository:
    def __init__(self, temporary_segments=None) -> None:
        self.temporary_segments = temporary_segments or []
        self.source_updates = []
        self.inserted_temporary_segments = []

    async def list_temporary_segments(self, transcript_id):
        return self.temporary_segments

    async def update_realtime_recording_source(self, transcript_id, user_id, update):
        self.source_updates.append((transcript_id, user_id, update))
        return True

    async def insert_temporary_segment(self, transcript_id, segment):
        self.inserted_temporary_segments.append((transcript_id, segment))


def test_save_realtime_transcript_uses_existing_temporary_segments() -> None:
    user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("11111111-1111-1111-1111-111111111111")
    repository = FakeRealtimeRepository(
        temporary_segments=[
            TemporarySegmentDetail(
                id=None,
                transcript_id=transcript_id,
                segment_index=0,
                start_seconds=None,
                end_seconds=None,
                text="server final",
            )
        ]
    )
    storage = FakeStorageService()

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[realtime.get_rag_repository] = lambda: repository
    app.dependency_overrides[realtime.get_upload_storage_service] = lambda: storage

    try:
        response = client.post(
            "/audio/transcripts/realtime",
            data={
                "transcript_id": str(transcript_id),
                "title": "Realtime lecture",
                "duration_seconds": "12.5",
                "segments": "not-json",
            },
            files={"file": ("recording.wav", b"fake wav", "audio/wav")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "transcript_id": str(transcript_id),
        "segment_count": 1,
        "file_uri": f"/uploads/{user_id}/recording.wav",
        "status": "uploaded",
        "content_status": "pending",
        "index_status": "pending",
    }
    assert repository.inserted_temporary_segments == []
    assert repository.source_updates[0][2].file_uri == f"/uploads/{user_id}/recording.wav"
    assert repository.source_updates[0][2].original_filename == "recording.wav"


def test_save_realtime_transcript_writes_client_segments_as_fallback() -> None:
    user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transcript_id = UUID("22222222-2222-2222-2222-222222222222")
    repository = FakeRealtimeRepository()
    storage = FakeStorageService()

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[realtime.get_rag_repository] = lambda: repository
    app.dependency_overrides[realtime.get_upload_storage_service] = lambda: storage

    try:
        response = client.post(
            "/audio/transcripts/realtime",
            data={
                "transcript_id": str(transcript_id),
                "title": "Realtime lecture",
                "duration_seconds": "20",
                "segments": json.dumps(
                    [
                        {
                            "segment_index": 0,
                            "start_seconds": 0,
                            "end_seconds": 4,
                            "text": "first",
                        },
                        {
                            "segment_index": 1,
                            "start_seconds": 4,
                            "end_seconds": 8,
                            "text": "second",
                        },
                    ]
                ),
            },
            files={"file": ("recording.wav", b"fake wav", "audio/wav")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["segment_count"] == 2
    assert response.json()["status"] == "uploaded"
    assert response.json()["content_status"] == "pending"
    assert response.json()["index_status"] == "pending"
    assert [item[1].text for item in repository.inserted_temporary_segments] == [
        "first",
        "second",
    ]
    assert repository.inserted_temporary_segments[0][1].raw_metadata == {
        "source": "client_realtime_fallback"
    }
