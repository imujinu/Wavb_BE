from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, UploadFile

from repositories.rag_repository import RagRepository
from schemas.rag import DomainType, SegmentCreate, TranscriptCreate, TranscriptResultUpdate
from services.chunk_builder import get_chunk_builder
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
    ) -> None:
        self._repository = repository
        self._transcription_service = transcription_service or TranscriptionService()

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
            # 도메인 별 청크 빌딩 후 저장
            chunks = get_chunk_builder(domain_type).build(segments)
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
