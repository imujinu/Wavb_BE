from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from services.context_chunk_planning_service import ContextChunkPlanGroup
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


class FakeChunkMetadataService:
    def __init__(self, exception=None) -> None:
        self._exception = exception
        self.received_chunks = []

    async def enrich_chunks(self, chunks):
        self.received_chunks.append(chunks)
        if self._exception:
            raise self._exception
        return [
            chunk.model_copy(
                update={
                    "topic": "출시 일정",
                    "keywords": ["출시", "일정"],
                    "summary": "출시 일정 논의 요약",
                    "metadata": {**chunk.metadata, "decision_items": ["다음 주 출시"]},
                }
            )
            for chunk in chunks
        ]


class FakeContextChunkPlanningService:
    def __init__(self, groups=None, exception=None) -> None:
        self._groups = groups
        self._exception = exception
        self.calls = []

    async def plan_chunks(self, domain_type, segments):
        self.calls.append((domain_type, segments))
        if self._exception:
            raise self._exception
        if self._groups is not None:
            return self._groups
        return [
            ContextChunkPlanGroup(
                segments[0].segment_index,
                segments[-1].segment_index,
                "전체 맥락",
                "테스트용 LLM plan",
                "전체 segment를 하나의 맥락으로 묶음",
            )
        ]


@pytest.mark.asyncio
async def test_ingest_upload_persists_transcript_result_and_segments() -> None:
    repository = FakeRepository()
    planning_service = FakeContextChunkPlanningService()
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
    service = TranscriptIngestionService(
        repository,
        transcription_service,
        context_chunk_planning_service=planning_service,
    )

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
    assert repository.inserted_chunks[0][1][0].chunk_strategy == "meeting_context_plan_v1"
    assert repository.inserted_chunks[0][1][0].metadata["planning_method"] == "llm"
    assert planning_service.calls[0][0] == "meeting"
    assert result.segment_count == 2
    assert result.chunk_count == 1


@pytest.mark.asyncio
async def test_ingest_upload_enriches_chunks_before_insert() -> None:
    repository = FakeRepository()
    transcription_service = FakeTranscriptionService(
        TranscriptionResult(
            text="출시 일정을 논의했습니다.",
            duration_seconds=8.0,
            stt_model="whisper-1",
            segments=[TranscriptionSegment("출시 일정을 논의했습니다.", 0.0, 8.0)],
        )
    )
    metadata_service = FakeChunkMetadataService()
    planning_service = FakeContextChunkPlanningService()
    service = TranscriptIngestionService(
        repository,
        transcription_service,
        metadata_service,
        context_chunk_planning_service=planning_service,
    )

    await service.ingest_upload(FakeUploadFile(), domain_type="meeting")

    inserted_chunk = repository.inserted_chunks[0][1][0]
    assert metadata_service.received_chunks[0][0].chunk_strategy == "meeting_context_plan_v1"
    assert inserted_chunk.segment_start_index == 0
    assert inserted_chunk.segment_end_index == 0
    assert inserted_chunk.topic == "출시 일정"
    assert inserted_chunk.keywords == ["출시", "일정"]
    assert inserted_chunk.summary == "출시 일정 논의 요약"
    assert inserted_chunk.metadata["decision_items"] == ["다음 주 출시"]


@pytest.mark.asyncio
async def test_ingest_upload_uses_original_chunks_when_metadata_enrichment_fails() -> None:
    repository = FakeRepository()
    transcription_service = FakeTranscriptionService(
        TranscriptionResult(
            text="강의 개념을 설명했습니다.",
            duration_seconds=8.0,
            stt_model="whisper-1",
            segments=[TranscriptionSegment("강의 개념을 설명했습니다.", 0.0, 8.0)],
        )
    )
    planning_service = FakeContextChunkPlanningService()
    service = TranscriptIngestionService(
        repository,
        transcription_service,
        FakeChunkMetadataService(exception=RuntimeError("metadata failed")),
        context_chunk_planning_service=planning_service,
    )

    result = await service.ingest_upload(FakeUploadFile(), domain_type="lecture")

    inserted_chunk = repository.inserted_chunks[0][1][0]
    assert repository.updates[0][1].status == "completed"
    assert inserted_chunk.topic is None
    assert inserted_chunk.chunk_strategy == "lecture_context_plan_v1"
    assert result.chunk_count == 1


@pytest.mark.asyncio
async def test_ingest_upload_uses_llm_plan_to_insert_multiple_context_chunks() -> None:
    repository = FakeRepository()
    transcription_service = FakeTranscriptionService(
        TranscriptionResult(
            text="첫 안건입니다. 계속 논의합니다. 다음 안건입니다. 마무리합니다.",
            duration_seconds=40.0,
            stt_model="whisper-1",
            segments=[
                TranscriptionSegment("첫 안건입니다.", 0.0, 8.0),
                TranscriptionSegment("계속 논의합니다.", 10.0, 18.0),
                TranscriptionSegment("다음 안건입니다.", 20.0, 28.0),
                TranscriptionSegment("마무리합니다.", 30.0, 38.0),
            ],
        )
    )
    planning_service = FakeContextChunkPlanningService(
        groups=[
            ContextChunkPlanGroup(0, 1, "첫 안건", "첫 안건 논의", "첫 안건 정리"),
            ContextChunkPlanGroup(2, 3, "다음 안건", "다음 안건 논의", "다음 안건 정리"),
        ]
    )
    service = TranscriptIngestionService(
        repository,
        transcription_service,
        context_chunk_planning_service=planning_service,
    )

    result = await service.ingest_upload(FakeUploadFile(), domain_type="meeting")

    inserted_chunks = repository.inserted_chunks[0][1]
    assert result.chunk_count == 2
    assert [chunk.segment_start_index for chunk in inserted_chunks] == [0, 2]
    assert [chunk.segment_end_index for chunk in inserted_chunks] == [1, 3]
    assert inserted_chunks[0].metadata["planning_method"] == "llm"
    assert inserted_chunks[0].metadata["planning_reason"] == "첫 안건 논의"


@pytest.mark.asyncio
async def test_ingest_upload_uses_fallback_chunks_when_planner_fails() -> None:
    repository = FakeRepository()
    transcription_service = FakeTranscriptionService(
        TranscriptionResult(
            text="첫 안건입니다. 계속 논의합니다. 다음 안건입니다. 마무리합니다.",
            duration_seconds=250.0,
            stt_model="whisper-1",
            segments=[
                TranscriptionSegment("첫 안건입니다.", 0.0, 60.0),
                TranscriptionSegment("계속 논의합니다.", 70.0, 120.0),
                TranscriptionSegment("다음 안건입니다.", 130.0, 200.0),
                TranscriptionSegment("마무리합니다.", 210.0, 250.0),
            ],
        )
    )
    planning_service = FakeContextChunkPlanningService(exception=RuntimeError("planner failed"))
    service = TranscriptIngestionService(
        repository,
        transcription_service,
        context_chunk_planning_service=planning_service,
    )

    result = await service.ingest_upload(FakeUploadFile(), domain_type="meeting")

    inserted_chunks = repository.inserted_chunks[0][1]
    assert result.chunk_count == 2
    assert [chunk.segment_start_index for chunk in inserted_chunks] == [0, 2]
    assert [chunk.segment_end_index for chunk in inserted_chunks] == [1, 3]
    assert inserted_chunks[0].chunk_strategy == "meeting_context_fallback_v1"
    assert inserted_chunks[0].metadata["planning_method"] == "fallback"


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
