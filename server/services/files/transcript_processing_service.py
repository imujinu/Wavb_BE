from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, status

from repositories.rag_repository import RagRepository
from schemas.rag import (
    SegmentCreate,
    TemporarySegmentDetail,
    TranscriptProcessingDetail,
    TranscriptProcessingStatusUpdate,
    TranscriptResultUpdate,
)
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.audio.transcription_service import TranscriptionResult, TranscriptionService
from services.files.document_text_extraction_service import DocumentTextExtractionService
from services.files.processing_cancellation import ProcessingCancelledError
from services.files.upload_storage_service import UploadStorageService


@dataclass(frozen=True)
class TranscriptProcessingResult:
    transcript_id: UUID
    status: str
    content_status: str
    index_status: str
    segment_count: int
    chunk_count: int


@dataclass(frozen=True)
class PreparedContent:
    text: str
    segments: list[SegmentCreate]
    duration_seconds: float | None
    stt_model: str | None


class TranscriptProcessingService:
    def __init__(
        self,
        repository: RagRepository,
        upload_storage_service: UploadStorageService | None = None,
        document_text_extraction_service: DocumentTextExtractionService | None = None,
        transcription_service: TranscriptionService | None = None,
        transcript_ingestion_service: TranscriptIngestionService | None = None,
    ) -> None:
        self._repository = repository
        self._upload_storage_service = upload_storage_service or UploadStorageService()
        self._document_text_extraction_service = (
            document_text_extraction_service or DocumentTextExtractionService()
        )
        self._transcription_service = transcription_service
        self._transcript_ingestion_service = transcript_ingestion_service

    # 기능 요약: 업로드된 파일을 사용자 요청 시점에 텍스트화하고 RAG 인덱스까지 생성한다.
    # 기능 흐름:
    #   1. transcript 소유권과 현재 상태를 조회한다.
    #   2. content_status가 pending이면 문서 추출/STT/임시 segment 승격을 수행한다.
    #   3. index_status가 pending이면 segment/chunk/search_chunks/embedding 저장을 수행한다.
    # 파라미터:
    #   transcript_id: 처리 대상 transcript UUID.
    #   user_id: 인증 사용자 UUID.
    async def process(
        self,
        transcript_id: UUID,
        user_id: UUID,
    ) -> TranscriptProcessingResult:
        transcript = await self._repository.get_transcript_for_processing(
            transcript_id,
            user_id,
        )
        if transcript is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transcript not found.",
            )

        if transcript.status in {"cancel_requested", "cancelled"}:
            await self._mark_cancelled(
                transcript.id,
                user_id,
                content_status=self._cancelled_content_status(transcript),
                index_status=self._cancelled_index_status(transcript),
            )
            return await self._build_current_result(transcript.id, user_id)

        if transcript.index_status == "completed":
            return TranscriptProcessingResult(
                transcript_id=transcript.id,
                status=transcript.status,
                content_status=transcript.content_status,
                index_status=transcript.index_status,
                segment_count=await self._repository.count_segments_by_transcript(transcript.id),
                chunk_count=await self._repository.count_chunks_by_transcript(transcript.id),
            )

        await self._set_status(
            transcript.id,
            user_id,
            status_value="processing",
            error_message=None,
        )

        if transcript.content_status == "completed":
            content = await self._load_completed_content(transcript)
        else:
            try:
                content = await self._prepare_content(transcript, user_id)
            except ProcessingCancelledError:
                return await self._build_current_result(transcript.id, user_id)

        try:
            await self._index_content(transcript.id, user_id, content.segments)
        except ProcessingCancelledError:
            return await self._build_current_result(transcript.id, user_id)

        segment_count = await self._repository.count_segments_by_transcript(transcript.id)
        chunk_count = await self._repository.count_chunks_by_transcript(transcript.id)
        return TranscriptProcessingResult(
            transcript_id=transcript.id,
            status="completed",
            content_status="completed",
            index_status="completed",
            segment_count=segment_count,
            chunk_count=chunk_count,
        )

    async def cancel(
        self,
        transcript_id: UUID,
        user_id: UUID,
    ) -> TranscriptProcessingResult:
        updated = await self._repository.request_processing_cancel(transcript_id, user_id)
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transcript not found.",
            )
        return await self._build_current_result(transcript_id, user_id)

    async def _prepare_content(
        self,
        transcript: TranscriptProcessingDetail,
        user_id: UUID,
    ) -> PreparedContent:
        await self._set_status(
            transcript.id,
            user_id,
            content_status="processing",
            index_status="pending",
            error_message=None,
        )

        try:
            await self._raise_if_cancel_requested(
                transcript.id,
                user_id,
                content_status="cancelled",
                index_status="cancelled",
            )
            content = await self._extract_or_transcribe(transcript)
            await self._raise_if_cancel_requested(
                transcript.id,
                user_id,
                content_status="cancelled",
                index_status="cancelled",
            )
            await self._repository.update_transcript_result(
                transcript.id,
                TranscriptResultUpdate(
                    full_text=content.text,
                    duration_seconds=content.duration_seconds,
                    stt_model=content.stt_model,
                    status="processing",
                ),
            )
            await self._set_status(
                transcript.id,
                user_id,
                status_value="processing",
                content_status="completed",
                error_message=None,
            )
            return content
        except ProcessingCancelledError:
            await self._mark_cancelled(
                transcript.id,
                user_id,
                content_status="cancelled",
                index_status="cancelled",
            )
            raise
        except Exception as exc:
            await self._set_status(
                transcript.id,
                user_id,
                status_value="failed",
                content_status="failed",
                error_message=str(getattr(exc, "detail", exc)),
            )
            raise

    async def _index_content(
        self,
        transcript_id: UUID,
        user_id: UUID,
        segments: list[SegmentCreate],
    ) -> None:
        await self._set_status(
            transcript_id,
            user_id,
            status_value="processing",
            index_status="processing",
            error_message=None,
        )
        try:
            await self._raise_if_cancel_requested(
                transcript_id,
                user_id,
                index_status="cancelled",
            )
            ingestion_service = (
                self._transcript_ingestion_service
                or TranscriptIngestionService(self._repository)
            )
            try:
                await ingestion_service.build_index_for_segments(
                    transcript_id,
                    segments,
                    cancellation_checker=self._cancellation_checker(transcript_id, user_id),
                )
            except TypeError as exc:
                if "cancellation_checker" not in str(exc):
                    raise
                await ingestion_service.build_index_for_segments(
                    transcript_id,
                    segments,
                )
            await self._raise_if_cancel_requested(
                transcript_id,
                user_id,
                index_status="cancelled",
            )
            await self._set_status(
                transcript_id,
                user_id,
                status_value="completed",
                index_status="completed",
                error_message=None,
            )
        except ProcessingCancelledError:
            await self._mark_cancelled(
                transcript_id,
                user_id,
                index_status="cancelled",
            )
            raise
        except Exception as exc:
            await self._set_status(
                transcript_id,
                user_id,
                status_value="failed",
                index_status="failed",
                error_message=str(getattr(exc, "detail", exc)),
            )
            raise

    async def _extract_or_transcribe(
        self,
        transcript: TranscriptProcessingDetail,
    ) -> PreparedContent:
        source_type = self._source_type(transcript)
        if source_type in {"pdf", "ppt"}:
            return await self._extract_document(transcript)
        if source_type == "audio":
            return await self._prepare_audio(transcript)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported transcript source type.",
        )

    async def _extract_document(
        self,
        transcript: TranscriptProcessingDetail,
    ) -> PreparedContent:
        path = self._upload_storage_service.resolve_uri(transcript.source_audio_uri)
        file_name = transcript.original_filename or path.name
        extraction = await self._document_text_extraction_service.extract_path(
            path,
            file_name,
        )
        return PreparedContent(
            text=extraction.text,
            segments=extraction.segments,
            duration_seconds=float(len(extraction.segments)),
            stt_model=None,
        )

    async def _prepare_audio(
        self,
        transcript: TranscriptProcessingDetail,
    ) -> PreparedContent:
        temporary_segments = await self._repository.list_temporary_segments(transcript.id)
        if temporary_segments:
            segments = self._temporary_to_segments(temporary_segments)
            text = " ".join(segment.text for segment in segments if segment.text.strip())
            return PreparedContent(
                text=text,
                segments=segments,
                duration_seconds=transcript.duration_seconds,
                stt_model=transcript.stt_model or "realtime",
            )

        path = self._upload_storage_service.resolve_uri(transcript.source_audio_uri)
        transcription_service = self._transcription_service or TranscriptionService()
        transcription = await transcription_service.transcribe_path(path)
        segments = self._transcription_to_segments(transcription)
        if not transcription.text.strip() or not segments:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Audio transcription result is empty.",
            )
        return PreparedContent(
            text=transcription.text,
            segments=segments,
            duration_seconds=transcription.duration_seconds,
            stt_model=transcription.stt_model,
        )

    async def _load_completed_content(
        self,
        transcript: TranscriptProcessingDetail,
    ) -> PreparedContent:
        segments = await self._repository.fetch_segments_by_transcript(transcript.id)
        if not segments:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Transcript content is completed but segments are missing.",
            )
        text = transcript.full_text or " ".join(segment.text for segment in segments)
        return PreparedContent(
            text=text,
            segments=segments,
            duration_seconds=transcript.duration_seconds,
            stt_model=transcript.stt_model,
        )

    def _temporary_to_segments(
        self,
        temporary_segments: list[TemporarySegmentDetail],
    ) -> list[SegmentCreate]:
        segments: list[SegmentCreate] = []
        previous_end_seconds = 0.0
        for temporary_segment in temporary_segments:
            start_seconds = (
                temporary_segment.start_seconds
                if temporary_segment.start_seconds is not None
                else previous_end_seconds
            )
            end_seconds = (
                temporary_segment.end_seconds
                if temporary_segment.end_seconds is not None
                else start_seconds
            )
            if end_seconds < start_seconds:
                end_seconds = start_seconds
            previous_end_seconds = end_seconds
            segments.append(
                SegmentCreate(
                    segment_index=len(segments),
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    text=temporary_segment.text,
                    raw_metadata={
                        **temporary_segment.raw_metadata,
                        "source": "temporary_segments",
                    },
                    source_type="audio",
                    source_start_seconds=start_seconds,
                    source_end_seconds=end_seconds,
                )
            )
        return segments

    def _transcription_to_segments(
        self,
        transcription: TranscriptionResult,
    ) -> list[SegmentCreate]:
        segments: list[SegmentCreate] = []
        previous_end_seconds = 0.0
        for index, segment in enumerate(transcription.segments):
            if not segment.text.strip():
                continue
            start_seconds = segment.start_seconds
            if start_seconds is None:
                start_seconds = previous_end_seconds
            end_seconds = segment.end_seconds
            if end_seconds is None or end_seconds < start_seconds:
                end_seconds = start_seconds
            previous_end_seconds = end_seconds
            segments.append(
                SegmentCreate(
                    segment_index=len(segments),
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    text=segment.text,
                    raw_metadata={
                        "provider": "openai",
                        "stt_model": transcription.stt_model,
                        "source_segment_index": index,
                    },
                    source_type="audio",
                    source_start_seconds=start_seconds,
                    source_end_seconds=end_seconds,
                )
            )
        return segments

    async def _set_status(
        self,
        transcript_id: UUID,
        user_id: UUID,
        status_value: str | None = None,
        content_status: str | None = None,
        index_status: str | None = None,
        error_message: str | None = None,
    ) -> None:
        await self._repository.update_processing_status(
            transcript_id,
            user_id,
            TranscriptProcessingStatusUpdate(
                status=status_value,
                content_status=content_status,
                index_status=index_status,
                error_message=error_message,
            ),
        )

    def _cancellation_checker(self, transcript_id: UUID, user_id: UUID):
        async def checker() -> bool:
            return await self._repository.is_processing_cancel_requested(
                transcript_id,
                user_id,
            )

        return checker

    async def _raise_if_cancel_requested(
        self,
        transcript_id: UUID,
        user_id: UUID,
        content_status: str | None = None,
        index_status: str | None = None,
    ) -> None:
        if not await self._repository.is_processing_cancel_requested(
            transcript_id,
            user_id,
        ):
            return
        await self._mark_cancelled(
            transcript_id,
            user_id,
            content_status=content_status,
            index_status=index_status,
        )
        raise ProcessingCancelledError("Processing was cancelled by user.")

    async def _mark_cancelled(
        self,
        transcript_id: UUID,
        user_id: UUID,
        content_status: str | None = None,
        index_status: str | None = None,
    ) -> None:
        await self._set_status(
            transcript_id,
            user_id,
            status_value="cancelled",
            content_status=content_status,
            index_status=index_status,
            error_message="Processing cancelled by user.",
        )

    async def _build_current_result(
        self,
        transcript_id: UUID,
        user_id: UUID,
    ) -> TranscriptProcessingResult:
        transcript = await self._repository.get_transcript_for_processing(
            transcript_id,
            user_id,
        )
        if transcript is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transcript not found.",
            )
        return TranscriptProcessingResult(
            transcript_id=transcript_id,
            status=transcript.status,
            content_status=transcript.content_status,
            index_status=transcript.index_status,
            segment_count=await self._repository.count_segments_by_transcript(transcript_id),
            chunk_count=await self._repository.count_chunks_by_transcript(transcript_id),
        )

    def _cancelled_content_status(
        self,
        transcript: TranscriptProcessingDetail,
    ) -> str | None:
        if transcript.content_status == "completed":
            return None
        return "cancelled"

    def _cancelled_index_status(
        self,
        transcript: TranscriptProcessingDetail,
    ) -> str | None:
        if transcript.index_status == "completed":
            return None
        return "cancelled"

    def _source_type(self, transcript: TranscriptProcessingDetail) -> str | None:
        if transcript.source_type:
            return transcript.source_type
        file_name = transcript.original_filename or transcript.source_audio_uri
        suffix = Path(file_name).suffix.lower()
        if suffix in {".m4a", ".mp3", ".wav", ".webm"}:
            return "audio"
        if suffix == ".pdf":
            return "pdf"
        if suffix in {".ppt", ".pptx"}:
            return "ppt"
        return None
