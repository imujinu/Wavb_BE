from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from services.transcript_ingestion_service import TranscriptIngestionService
from services.transcription_service import TranscriptionResult, TranscriptionSegment


class FakeRepository:
    def __init__(self) -> None:
        self.transcript_id = uuid4()
        self.created = []
        self.updates = []
        self.inserted_segments = []
        self.inserted_chunks = []

    async def create_transcript(self, transcript):
        self.created.append(transcript)
        return self.transcript_id

    async def update_transcript_result(self, transcript_id, update):
        self.updates.append((transcript_id, update))

    async def insert_segments(self, transcript_id, segments):
        self.inserted_segments.append((transcript_id, segments))

    async def insert_chunks(self, transcript_id, chunks):
        self.inserted_chunks.append((transcript_id, chunks))


class FakeUploadFile:
    filename = "meeting.mp3"
    content_type = "audio/mpeg"


class FakeTranscriptionService:
    def __init__(self, result=None, exception=None) -> None:
        self._result = result
        self._exception = exception

    async def transcribe_with_segments(self, file):
        if self._exception:
            raise self._exception
        return self._result


@pytest.mark.asyncio
async def test_ingest_upload_persists_transcript_result_and_segments() -> None:
    repository = FakeRepository()
    transcription_service = FakeTranscriptionService(
        TranscriptionResult(
            text="첫 번째 발화 두 번째 발화",
            duration_seconds=12.5,
            stt_model="whisper-1",
            segments=[
                TranscriptionSegment("첫 번째 발화", 0.0, 4.0),
                TranscriptionSegment("두 번째 발화", 4.0, 8.5),
            ],
        )
    )
    service = TranscriptIngestionService(repository, transcription_service)

    result = await service.ingest_upload(
        file=FakeUploadFile(),
        domain_type="meeting",
        title="주간 회의",
        user_id=uuid4(),
    )

    assert isinstance(result.transcript_id, UUID)
    assert repository.created[0].status == "processing"
    assert repository.created[0].source_audio_uri == "upload://meeting.mp3"
    assert repository.updates[0][1].status == "completed"
    assert repository.updates[0][1].full_text == "첫 번째 발화 두 번째 발화"
    assert repository.updates[0][1].duration_seconds == 12.5
    assert repository.inserted_segments[0][0] == repository.transcript_id
    assert [segment.segment_index for segment in repository.inserted_segments[0][1]] == [0, 1]
    assert repository.inserted_segments[0][1][0].raw_metadata["stt_model"] == "whisper-1"
    assert repository.inserted_chunks[0][0] == repository.transcript_id
    assert repository.inserted_chunks[0][1][0].chunk_strategy == "meeting_speaker_turn_v1"
    assert result.segment_count == 2
    assert result.chunk_count == 1


@pytest.mark.asyncio
async def test_ingest_upload_marks_transcript_failed_when_stt_fails() -> None:
    repository = FakeRepository()
    transcription_service = FakeTranscriptionService(
        exception=HTTPException(status_code=502, detail="provider failed")
    )
    service = TranscriptIngestionService(repository, transcription_service)

    with pytest.raises(HTTPException):
        await service.ingest_upload(FakeUploadFile(), domain_type="lecture")

    assert repository.created[0].status == "processing"
    assert repository.updates[0][1].status == "failed"
    assert repository.updates[0][1].error_message == "provider failed"
    assert repository.inserted_segments == []
    assert repository.inserted_chunks == []
