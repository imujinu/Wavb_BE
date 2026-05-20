from fastapi.testclient import TestClient

from main import app
from routes import audio


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
