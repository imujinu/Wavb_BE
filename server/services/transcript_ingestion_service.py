from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, UploadFile

from repositories.rag_repository import RagRepository
from schemas.rag import (
    ChunkCreate,
    DomainType,
    SegmentCreate,
    TranscriptCreate,
    TranscriptResultUpdate,
)
from services.chunk_builder import (
    ContextPlannedChunkBuilder,
    DeterministicFallbackChunkBuilder,
)
from services.context_chunk_planning_service import ContextChunkPlanningService
from services.chunk_metadata_service import ChunkMetadataService
from services.transcription_service import TranscriptionService, TranscriptionSegment


@dataclass(frozen=True)
class TranscriptIngestionResult:
    transcript_id: UUID
    transcript: str
    duration_seconds: float | None
    stt_model: str
    segment_count: int
    chunk_count: int


class TranscriptIngestionService:
    def __init__(
        self,
        repository: RagRepository,
        transcription_service: TranscriptionService | None = None,
        chunk_metadata_service: ChunkMetadataService | None = None,
        context_chunk_planning_service: ContextChunkPlanningService | None = None,
        planned_chunk_builder: ContextPlannedChunkBuilder | None = None,
        fallback_chunk_builder: DeterministicFallbackChunkBuilder | None = None,
    ) -> None:
        self._repository = repository
        self._transcription_service = transcription_service or TranscriptionService()
        self._chunk_metadata_service = chunk_metadata_service
        self._context_chunk_planning_service = context_chunk_planning_service
        self._planned_chunk_builder = planned_chunk_builder or ContextPlannedChunkBuilder()
        self._fallback_chunk_builder = fallback_chunk_builder or DeterministicFallbackChunkBuilder(
            planned_chunk_builder=self._planned_chunk_builder
        )

    # Create the transcript source row, run STT, and persist the reusable segments.
    async def ingest_upload(
        self,
        file: UploadFile,
        domain_type: DomainType,
        title: str | None = None,
        user_id: UUID | None = None,
    ) -> TranscriptIngestionResult:
        transcript_id = await self._repository.create_transcript(
            TranscriptCreate(
                user_id=user_id,
                domain_type=domain_type,
                title=title,
                source_audio_uri=self._source_audio_uri(file),
                original_filename=file.filename,
                mime_type=file.content_type,
                status="processing",
            )
        )

        try:
            transcription = await self._transcription_service.transcribe_with_segments(file)
            if not transcription.text.strip():
                raise HTTPException(
                    status_code=422,
                    detail="Audio transcription result is empty.",
                )
            await self._repository.update_transcript_result(
                transcript_id,
                TranscriptResultUpdate(
                    full_text=transcription.text,
                    duration_seconds=transcription.duration_seconds,
                    stt_model=transcription.stt_model,
                    status="completed",
                ),
            )
            segments = self._to_segment_creates(
                transcription.segments,
                stt_model=transcription.stt_model,
            )
            await self._repository.insert_segments(transcript_id, segments)
            chunks = await self._build_chunks(domain_type, segments)
            chunks = await self._enrich_chunks(chunks)
            await self._repository.insert_chunks(transcript_id, chunks)
        except Exception as exc:
            await self._repository.update_transcript_result(
                transcript_id,
                TranscriptResultUpdate(
                    status="failed",
                    error_message=str(getattr(exc, "detail", exc)),
                ),
            )
            raise

        return TranscriptIngestionResult(
            transcript_id=transcript_id,
            transcript=transcription.text,
            duration_seconds=transcription.duration_seconds,
            stt_model=transcription.stt_model,
            segment_count=len(segments),
            chunk_count=len(chunks),
        )

    # LLM planner로 맥락 경계를 먼저 정하고, plan을 chunks 테이블 입력 모델로 변환합니다.
    # planner 초기화나 호출, plan 변환이 실패하면 deterministic fallback chunk를 저장합니다.
    async def _build_chunks(
        self,
        domain_type: DomainType,
        segments: list[SegmentCreate],
    ) -> list[ChunkCreate]:
        try:
            planning_service = (
                self._context_chunk_planning_service or ContextChunkPlanningService()
            )
            plan_groups = await planning_service.plan_chunks(domain_type, segments)
            return self._planned_chunk_builder.build(domain_type, segments, plan_groups)
        except Exception:
            return self._fallback_chunk_builder.build(domain_type, segments)

    # chunk metadata를 생성합니다.
    # OpenAI 설정이 없거나 생성 중 오류가 나면 원본 chunk를 반환해 transcript 저장 흐름을 유지합니다.
    async def _enrich_chunks(self, chunks: list[ChunkCreate]) -> list[ChunkCreate]:
        try:
            metadata_service = self._chunk_metadata_service or ChunkMetadataService()
            return await metadata_service.enrich_chunks(chunks)
        except Exception:
            return chunks

    def _source_audio_uri(self, file: UploadFile) -> str:
        filename = file.filename or "audio"
        return f"upload://{filename}"

    def _to_segment_creates(
        self,
        segments: list[TranscriptionSegment],
        stt_model: str,
    ) -> list[SegmentCreate]:
        segment_creates: list[SegmentCreate] = []
        previous_end_seconds = 0.0

        for index, segment in enumerate(segments):
            if not segment.text.strip():
                continue
            start_seconds = segment.start_seconds
            if start_seconds is None:
                start_seconds = previous_end_seconds
            end_seconds = segment.end_seconds
            if end_seconds is None or end_seconds < start_seconds:
                end_seconds = start_seconds
            previous_end_seconds = end_seconds
            segment_creates.append(
                SegmentCreate(
                    segment_index=len(segment_creates),
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    text=segment.text,
                    raw_metadata={
                        "provider": "openai",
                        "stt_model": stt_model,
                        "source_segment_index": index,
                    },
                )
            )

        return segment_creates
