from pathlib import Path
from uuid import uuid4

import pytest

from schemas.rag import (
    ChunkCreate,
    SegmentCreate,
    TemporarySegmentDetail,
    TranscriptProcessingDetail,
)
from services.files.document_text_extraction_service import DocumentTextExtractionResult
from services.files.transcript_processing_service import TranscriptProcessingService


class FakeRepository:
    def __init__(self, transcript: TranscriptProcessingDetail) -> None:
        self.transcript = transcript
        self.status_updates = []
        self.result_updates = []
        self.temporary_segments: list[TemporarySegmentDetail] = []
        self.saved_segment_count = 2
        self.saved_chunk_count = 1
        self.cancel_requested = False

    async def get_transcript_for_processing(self, transcript_id, user_id):
        if transcript_id == self.transcript.id and user_id == self.transcript.user_id:
            return self.transcript
        return None

    async def update_processing_status(self, transcript_id, user_id, update):
        self.status_updates.append((transcript_id, user_id, update))
        updates = {}
        if update.status is not None:
            updates["status"] = update.status
        if update.content_status is not None:
            updates["content_status"] = update.content_status
        if update.index_status is not None:
            updates["index_status"] = update.index_status
        if update.error_message is not None:
            updates["error_message"] = update.error_message
        if updates:
            self.transcript = self.transcript.model_copy(update=updates)
        return True

    async def update_transcript_result(self, transcript_id, update):
        self.result_updates.append((transcript_id, update))
        updates = {}
        if update.full_text is not None:
            updates["full_text"] = update.full_text
        if update.duration_seconds is not None:
            updates["duration_seconds"] = update.duration_seconds
        if update.stt_model is not None:
            updates["stt_model"] = update.stt_model
        if update.status is not None:
            updates["status"] = update.status
        if updates:
            self.transcript = self.transcript.model_copy(update=updates)

    async def request_processing_cancel(self, transcript_id, user_id):
        if transcript_id != self.transcript.id or user_id != self.transcript.user_id:
            return False
        self.cancel_requested = True
        self.transcript = self.transcript.model_copy(update={"status": "cancel_requested"})
        return True

    async def is_processing_cancel_requested(self, transcript_id, user_id):
        return (
            self.cancel_requested
            and transcript_id == self.transcript.id
            and user_id == self.transcript.user_id
        )

    async def list_temporary_segments(self, transcript_id):
        return self.temporary_segments

    async def fetch_segments_by_transcript(self, transcript_id):
        return []

    async def count_segments_by_transcript(self, transcript_id):
        return self.saved_segment_count

    async def count_chunks_by_transcript(self, transcript_id):
        return self.saved_chunk_count


class FakeUploadStorageService:
    def __init__(self) -> None:
        self.uris = []

    def resolve_uri(self, uri: str) -> Path:
        self.uris.append(uri)
        return Path("uploads/test/lecture.pdf")


class FakeDocumentTextExtractionService:
    def __init__(self) -> None:
        self.calls = []

    async def extract_path(self, path: Path, file_name: str):
        self.calls.append((path, file_name))
        return DocumentTextExtractionResult(
            text="문서 텍스트",
            source_type="pdf",
            segments=[
                SegmentCreate(
                    segment_index=0,
                    start_seconds=0.0,
                    end_seconds=1.0,
                    text="문서 텍스트",
                    source_type="pdf",
                    source_page_start=1,
                    source_page_end=1,
                )
            ],
        )


class FakeTranscriptIngestionService:
    def __init__(self) -> None:
        self.index_calls = []

    async def build_index_for_segments(self, transcript_id, segments):
        self.index_calls.append((transcript_id, segments))
        return [
            ChunkCreate(
                chunk_index=0,
                chunk_strategy="test",
                text=segments[0].text,
            )
        ]


class FailingTranscriptionService:
    async def transcribe_path(self, path: Path):
        raise AssertionError("STT should not be called when temporary segments exist")


def _make_transcript(source_type: str = "pdf") -> TranscriptProcessingDetail:
    return TranscriptProcessingDetail(
        id=uuid4(),
        user_id=uuid4(),
        title="lecture",
        source_audio_uri="/uploads/user/file.pdf",
        original_filename="lecture.pdf",
        mime_type="application/pdf",
        status="uploaded",
        source_type=source_type,
        content_status="pending",
        index_status="pending",
    )


@pytest.mark.asyncio
async def test_process_document_extracts_text_and_indexes() -> None:
    transcript = _make_transcript("pdf")
    repository = FakeRepository(transcript)
    extraction_service = FakeDocumentTextExtractionService()
    ingestion_service = FakeTranscriptIngestionService()
    service = TranscriptProcessingService(
        repository=repository,
        upload_storage_service=FakeUploadStorageService(),
        document_text_extraction_service=extraction_service,
        transcript_ingestion_service=ingestion_service,
    )

    result = await service.process(transcript.id, transcript.user_id)

    assert result.status == "completed"
    assert result.content_status == "completed"
    assert result.index_status == "completed"
    assert extraction_service.calls[0][1] == "lecture.pdf"
    assert repository.result_updates[0][1].full_text == "문서 텍스트"
    assert ingestion_service.index_calls[0][0] == transcript.id
    assert ingestion_service.index_calls[0][1][0].source_page_start == 1


@pytest.mark.asyncio
async def test_process_stops_before_content_when_cancel_requested() -> None:
    transcript = _make_transcript("pdf")
    repository = FakeRepository(transcript)
    repository.cancel_requested = True
    extraction_service = FakeDocumentTextExtractionService()
    ingestion_service = FakeTranscriptIngestionService()
    service = TranscriptProcessingService(
        repository=repository,
        upload_storage_service=FakeUploadStorageService(),
        document_text_extraction_service=extraction_service,
        transcript_ingestion_service=ingestion_service,
    )

    result = await service.process(transcript.id, transcript.user_id)

    assert result.status == "cancelled"
    assert result.content_status == "cancelled"
    assert result.index_status == "cancelled"
    assert extraction_service.calls == []
    assert ingestion_service.index_calls == []


@pytest.mark.asyncio
async def test_process_audio_uses_temporary_segments_before_stt() -> None:
    transcript = _make_transcript("audio").model_copy(
        update={
            "source_audio_uri": "/uploads/user/recording.webm",
            "original_filename": "recording.webm",
            "mime_type": "audio/webm",
        }
    )
    repository = FakeRepository(transcript)
    repository.temporary_segments = [
        TemporarySegmentDetail(
            id=uuid4(),
            transcript_id=transcript.id,
            segment_index=0,
            start_seconds=None,
            end_seconds=None,
            text="실시간 전사",
            raw_metadata={"provider": "deepgram"},
        )
    ]
    ingestion_service = FakeTranscriptIngestionService()
    service = TranscriptProcessingService(
        repository=repository,
        upload_storage_service=FakeUploadStorageService(),
        transcription_service=FailingTranscriptionService(),
        transcript_ingestion_service=ingestion_service,
    )

    result = await service.process(transcript.id, transcript.user_id)

    assert result.status == "completed"
    assert repository.result_updates[0][1].full_text == "실시간 전사"
    assert ingestion_service.index_calls[0][1][0].raw_metadata["provider"] == "deepgram"
