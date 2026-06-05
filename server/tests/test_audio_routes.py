from uuid import UUID

from fastapi.testclient import TestClient

from dependencies.auth import get_current_user
from main import app
from routes import audio
from schemas.auth import CurrentUser
from schemas.rag import LectureSummaryResponse


client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_audio_summarize_rejects_missing_file() -> None:
    response = client.post("/audio/summarize")

    assert response.status_code == 422


def test_audio_summarize_rejects_unsupported_extension() -> None:
    response = client.post(
        "/audio/summarize",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 400
    assert "Unsupported audio file type" in response.json()["detail"]


def test_audio_summarize_returns_transcript_and_summary(monkeypatch) -> None:
    class FakeTranscriptionService:
        async def transcribe(self, file):
            return "오늘 회의에서는 출시 일정과 테스트 계획을 논의했습니다."

    class FakeSummaryService:
        async def summarize(self, transcript):
            return "출시 일정과 테스트 계획을 논의했습니다."

    monkeypatch.setattr(audio, "TranscriptionService", FakeTranscriptionService)
    monkeypatch.setattr(audio, "SummaryService", FakeSummaryService)

    response = client.post(
        "/audio/summarize",
        files={"file": ("meeting.m4a", b"fake audio", "audio/mp4")},
    )

    assert response.status_code == 200
    assert response.json() == {
        "transcript": "오늘 회의에서는 출시 일정과 테스트 계획을 논의했습니다.",
        "summary": "출시 일정과 테스트 계획을 논의했습니다.",
    }


def test_audio_summarize_rejects_empty_transcript(monkeypatch) -> None:
    class FakeTranscriptionService:
        async def transcribe(self, file):
            return " "

    class FakeSummaryService:
        async def summarize(self, transcript):
            return "should not be called"

    monkeypatch.setattr(audio, "TranscriptionService", FakeTranscriptionService)
    monkeypatch.setattr(audio, "SummaryService", FakeSummaryService)

    response = client.post(
        "/audio/summarize",
        files={"file": ("empty.wav", b"fake audio", "audio/wav")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Audio transcription result is empty."


def test_audio_transcripts_persists_stt_flow(monkeypatch) -> None:
    expected_transcript_id = "11111111-1111-1111-1111-111111111111"
    # 테스트용 고정 user_id — JWT 토큰 없이 의존성 override로 주입
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    class FakeIngestionResult:
        transcript_id = expected_transcript_id
        transcript = "회의 내용을 저장했습니다."
        duration_seconds = 5.5
        stt_model = "whisper-1"
        segment_count = 1

    class FakeTranscriptIngestionService:
        def __init__(self, repository):
            self.repository = repository

        async def ingest_upload(
            self, file, file_uri, file_name, user_id=None
        ):
            assert file_name == "주간회의.mp3"
            # JWT에서 주입된 user_id가 올바르게 전달됐는지 검증
            assert user_id == fake_user_id
            return FakeIngestionResult()

    # get_current_user 의존성을 테스트용 CurrentUser로 override
    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[audio.get_rag_repository] = lambda: object()
    app.dependency_overrides[get_current_user] = fake_current_user
    monkeypatch.setattr(
        audio,
        "TranscriptIngestionService",
        FakeTranscriptIngestionService,
    )

    try:
        response = client.post(
            "/audio/transcripts",
            data={
                "file_uri": "upload://주간회의.mp3",
                "file_name": "주간회의.mp3",
            },
            files={"file": ("주간회의.mp3", b"fake audio", "audio/mpeg")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "transcript_id": expected_transcript_id,
        "transcript": "회의 내용을 저장했습니다.",
        "duration_seconds": 5.5,
        "stt_model": "whisper-1",
        "segment_count": 1,
        "status": "completed",
    }


def test_create_lecture_summary_route_uses_authenticated_user() -> None:
    transcript_id = UUID("22222222-2222-2222-2222-222222222222")
    fake_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    class FakeLectureSummaryService:
        async def get_or_create_summary(self, received_transcript_id, received_user_id):
            assert received_transcript_id == transcript_id
            assert received_user_id == fake_user_id
            return LectureSummaryResponse(
                summary_id=UUID("33333333-3333-3333-3333-333333333333"),
                transcript_id=received_transcript_id,
                persona_id="general",
                overview={
                    "title": "강의",
                    "summary": "강의 요약",
                    "key_points": [],
                },
                contexts=[],
                keywords=[],
            )

    def fake_current_user() -> CurrentUser:
        return CurrentUser(user_id=fake_user_id, email="test@example.com")

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[audio.get_lecture_summary_service] = (
        lambda: FakeLectureSummaryService()
    )

    try:
        response = client.post(f"/audio/transcripts/{transcript_id}/summary")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["summary_id"] == "33333333-3333-3333-3333-333333333333"
    assert body["persona_id"] == "general"
    assert body["overview"]["summary"] == "강의 요약"
    assert body["contexts"] == []
    assert body["keywords"] == []
