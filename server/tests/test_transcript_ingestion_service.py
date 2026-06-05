from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from schemas.rag import ChunkRow
from services.chunks.chunk_builder import DeterministicFallbackChunkBuilder
from services.chunks.context_chunk_planning_service import ContextChunkPlanGroup
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.audio.transcription_service import TranscriptionResult, TranscriptionSegment


class FakeRepository:
    def __init__(self) -> None:
        self.transcript_id = uuid4()
        self.created = []
        self.updates = []
        self.inserted_segments = []
        self.inserted_chunks = []
        self.inserted_search_chunks = []
        # fetch_chunks_by_transcript 반환값 — 테스트에서 덮어쓸 수 있음
        self._chunk_rows: list[ChunkRow] = []

    async def create_transcript(self, transcript):
        self.created.append(transcript)
        return self.transcript_id

    async def update_transcript_result(self, transcript_id, update):
        self.updates.append((transcript_id, update))

    async def insert_segments(self, transcript_id, segments):
        self.inserted_segments.append((transcript_id, segments))

    async def insert_chunks(self, transcript_id, chunks):
        self.inserted_chunks.append((transcript_id, chunks))

    async def fetch_chunks_by_transcript(self, _):
        return self._chunk_rows

    async def insert_search_chunks(self, transcript_id, search_chunks):
        self.inserted_search_chunks.append((transcript_id, search_chunks))


class FakeUploadFile:
    filename = "meeting.mp3"
    content_type = "audio/mpeg"


class FakeTranscriptionService:
    def __init__(self, result=None, exception=None) -> None:
        self._result = result
        self._exception = exception

    async def transcribe_with_segments(self, file, language="ko"):
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
                    "metadata": {
                        **chunk.metadata,
                        "concepts": ["출시 일정"],
                        "learning_points": ["일정 확인"],
                    },
                }
            )
            for chunk in chunks
        ]


class FakeContextChunkPlanningService:
    def __init__(self, groups=None, exception=None) -> None:
        self._groups = groups
        self._exception = exception
        self.calls = []

    async def plan_chunks(self, segments):
        self.calls.append(segments)
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
    folder_id = uuid4()

    result = await service.ingest_upload(
        file=FakeUploadFile(),
        file_uri="upload://meeting.mp3",
        file_name="meeting.mp3",
        user_id=uuid4(),
        folder_id=folder_id,
    )

    assert isinstance(result.transcript_id, UUID)
    assert repository.created[0].status == "processing"
    assert repository.created[0].folder_id == folder_id
    assert repository.created[0].source_audio_uri == "upload://meeting.mp3"
    assert repository.updates[0][1].status == "completed"
    assert repository.updates[0][1].full_text == "첫 번째 발화 두 번째 발화"
    assert repository.updates[0][1].duration_seconds == 12.5
    assert repository.inserted_segments[0][0] == repository.transcript_id
    assert [segment.segment_index for segment in repository.inserted_segments[0][1]] == [0, 1]
    assert repository.inserted_segments[0][1][0].raw_metadata["stt_model"] == "whisper-1"
    assert repository.inserted_segments[0][1][0].source_type == "audio"
    assert repository.inserted_segments[0][1][0].source_start_seconds == 0.0
    assert repository.inserted_segments[0][1][0].source_end_seconds == 4.0
    assert repository.inserted_chunks[0][0] == repository.transcript_id
    assert repository.inserted_chunks[0][1][0].chunk_strategy == "lecture_context_plan_v1"
    assert repository.inserted_chunks[0][1][0].metadata["planning_method"] == "llm"
    assert planning_service.calls[0][0].segment_index == 0
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

    await service.ingest_upload(
        FakeUploadFile(),
        file_uri="upload://meeting.mp3",
        file_name="meeting.mp3",
    )

    inserted_chunk = repository.inserted_chunks[0][1][0]
    assert metadata_service.received_chunks[0][0].chunk_strategy == "lecture_context_plan_v1"
    assert inserted_chunk.segment_start_index == 0
    assert inserted_chunk.segment_end_index == 0
    assert inserted_chunk.topic == "출시 일정"
    assert inserted_chunk.keywords == ["출시", "일정"]
    assert inserted_chunk.summary == "출시 일정 논의 요약"
    assert inserted_chunk.metadata["concepts"] == ["출시 일정"]
    assert inserted_chunk.metadata["learning_points"] == ["일정 확인"]


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

    result = await service.ingest_upload(
        FakeUploadFile(),
        file_uri="upload://lecture.mp3",
        file_name="lecture.mp3",
    )

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

    result = await service.ingest_upload(
        FakeUploadFile(),
        file_uri="upload://meeting.mp3",
        file_name="meeting.mp3",
    )

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
    fallback_builder = DeterministicFallbackChunkBuilder(
        lecture_max_seconds=180.0,
    )
    service = TranscriptIngestionService(
        repository,
        transcription_service,
        context_chunk_planning_service=planning_service,
        fallback_chunk_builder=fallback_builder,
    )

    result = await service.ingest_upload(
        FakeUploadFile(),
        file_uri="upload://meeting.mp3",
        file_name="meeting.mp3",
    )

    inserted_chunks = repository.inserted_chunks[0][1]
    assert result.chunk_count == 2
    assert [chunk.segment_start_index for chunk in inserted_chunks] == [0, 2]
    assert [chunk.segment_end_index for chunk in inserted_chunks] == [1, 3]
    assert inserted_chunks[0].chunk_strategy == "lecture_context_fallback_v1"
    assert inserted_chunks[0].metadata["planning_method"] == "fallback"


@pytest.mark.asyncio
async def test_ingest_upload_marks_transcript_failed_when_stt_fails() -> None:
    repository = FakeRepository()
    transcription_service = FakeTranscriptionService(
        exception=HTTPException(status_code=502, detail="provider failed")
    )
    service = TranscriptIngestionService(repository, transcription_service)

    with pytest.raises(HTTPException):
        await service.ingest_upload(
            FakeUploadFile(),
            file_uri="upload://lecture.mp3",
            file_name="lecture.mp3",
        )

    assert repository.created[0].status == "processing"
    assert repository.updates[0][1].status == "failed"
    assert repository.updates[0][1].error_message == "provider failed"
    assert repository.inserted_segments == []
    assert repository.inserted_chunks == []


# ──────────────────────────────────────────────
# search chunk indexing 통합 테스트
# ──────────────────────────────────────────────

class FakeSearchChunkBuilder:
    """SearchChunkBuilder 의존성 없이 결정론적 child unit을 반환하는 가짜 구현체."""

    def build(self, parent_chunks, _):
        from schemas.rag import SearchChunkCreate
        # parent chunk마다 child 1개씩 생성
        return [
            SearchChunkCreate(
                parent_chunk_id=chunk.id,
                child_index=0,
                segment_start_index=chunk.segment_start_index or 0,
                segment_end_index=chunk.segment_end_index or 0,
                start_seconds=chunk.start_seconds,
                end_seconds=chunk.end_seconds,
                text=chunk.text,
                text_morphemes=f"morpheme {chunk.chunk_index}",
            )
            for chunk in parent_chunks
        ]


class FakeEmbeddingService:
    """EmbeddingService를 대체하는 가짜 구현체 — 텍스트 수만큼 더미 벡터를 반환한다."""

    def __init__(self, exception=None) -> None:
        self._exception = exception
        self.received_texts: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.received_texts.append(texts)
        if self._exception:
            raise self._exception
        # 1536차원 더미 벡터 반환
        return [[0.1] * 1536 for _ in texts]


def _make_chunk_row(segment_start: int, segment_end: int) -> ChunkRow:
    """테스트용 ChunkRow 생성 헬퍼."""
    return ChunkRow(
        id=uuid4(),
        chunk_index=segment_start,
        segment_start_index=segment_start,
        segment_end_index=segment_end,
        start_seconds=float(segment_start * 4),
        end_seconds=float(segment_end * 4 + 4),
        text=f"segment {segment_start}~{segment_end} 텍스트",
        metadata={},
    )


@pytest.mark.asyncio
async def test_ingest_creates_transcript_and_saves_chunks_and_search_chunks() -> None:
    """chunks 저장과 search_chunks 저장이 모두 호출되고 transcript가 completed 상태인지 검증."""
    repository = FakeRepository()
    # fetch_chunks_by_transcript가 반환할 parent chunk rows 설정
    repository._chunk_rows = [_make_chunk_row(0, 1)]

    transcription_service = FakeTranscriptionService(
        TranscriptionResult(
            text="첫 번째 발화 두 번째 발화",
            duration_seconds=10.0,
            stt_model="whisper-1",
            segments=[
                TranscriptionSegment("첫 번째 발화", 0.0, 4.0),
                TranscriptionSegment("두 번째 발화", 4.0, 8.0),
            ],
        )
    )
    embedding_service = FakeEmbeddingService()
    search_chunk_builder = FakeSearchChunkBuilder()
    planning_service = FakeContextChunkPlanningService()

    service = TranscriptIngestionService(
        repository,
        transcription_service,
        context_chunk_planning_service=planning_service,
        search_chunk_builder=search_chunk_builder,
        embedding_service=embedding_service,
    )

    result = await service.ingest_upload(
        FakeUploadFile(),
        file_uri="upload://meeting.mp3",
        file_name="meeting.mp3",
    )

    # transcript 완료 상태 확인
    assert repository.updates[0][1].status == "completed"
    # chunks 저장 확인
    assert len(repository.inserted_chunks) == 1
    # search_chunks 저장 확인
    assert len(repository.inserted_search_chunks) == 1
    transcript_id, saved_search_chunks = repository.inserted_search_chunks[0]
    assert transcript_id == repository.transcript_id
    assert len(saved_search_chunks) == 1
    # embedding이 첨부되어 있는지 확인
    assert saved_search_chunks[0].embedding == [0.1] * 1536
    assert saved_search_chunks[0].embedding_model == "text-embedding-3-small"
    assert saved_search_chunks[0].text_morphemes == "morpheme 0"
    # 결과 객체 정합성 확인
    assert isinstance(result.transcript_id, UUID)
    assert result.segment_count == 2


@pytest.mark.asyncio
async def test_ingest_continues_on_search_chunk_failure() -> None:
    """embedding 실패 시 transcript가 completed 상태를 유지하고 에러 로그가 기록되는지 검증."""
    repository = FakeRepository()
    repository._chunk_rows = [_make_chunk_row(0, 0)]

    transcription_service = FakeTranscriptionService(
        TranscriptionResult(
            text="발화 내용입니다.",
            duration_seconds=5.0,
            stt_model="whisper-1",
            segments=[TranscriptionSegment("발화 내용입니다.", 0.0, 5.0)],
        )
    )
    # embedding 단계에서 예외 발생하도록 설정
    embedding_service = FakeEmbeddingService(exception=RuntimeError("embedding API failed"))
    planning_service = FakeContextChunkPlanningService()

    service = TranscriptIngestionService(
        repository,
        transcription_service,
        context_chunk_planning_service=planning_service,
        search_chunk_builder=FakeSearchChunkBuilder(),
        embedding_service=embedding_service,
    )

    # ingest_upload는 예외 없이 완료되어야 함
    result = await service.ingest_upload(
        FakeUploadFile(),
        file_uri="upload://meeting.mp3",
        file_name="meeting.mp3",
    )

    # transcript는 completed 상태 유지
    assert repository.updates[0][1].status == "completed"
    # search_chunks 저장은 호출되지 않음 (embedding 실패로 중단)
    assert repository.inserted_search_chunks == []
    # chunks 저장은 정상 완료
    assert len(repository.inserted_chunks) == 1
    assert result.chunk_count == 1
