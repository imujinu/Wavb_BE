import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, UploadFile

from repositories.rag_repository import RagRepository
from schemas.rag import (
    ChunkCreate,
    SearchChunkCreate,
    SegmentCreate,
    TranscriptCreate,
    TranscriptResultUpdate,
)
from services.chunks.chunk_builder import (
    ContextPlannedChunkBuilder,
    DeterministicFallbackChunkBuilder,
)
from services.chunks.context_chunk_planning_service import ContextChunkPlanningService
from services.chunks.chunk_metadata_service import ChunkMetadataService
from services.rag.embedding_service import EmbeddingService
from services.rag.morpheme_service import MorphemeService
from services.chunks.search_chunk_builder import SearchChunkBuilder
from services.audio.transcription_service import TranscriptionService, TranscriptionSegment
from services.files.processing_cancellation import raise_if_cancel_requested
from settings import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptIngestionResult:
    transcript_id: UUID
    transcript: str
    duration_seconds: float | None
    stt_model: str
    segment_count: int
    chunk_count: int
    processing_seconds: float


class TranscriptIngestionService:
    def __init__(
        self,
        repository: RagRepository,
        transcription_service: TranscriptionService | None = None,
        chunk_metadata_service: ChunkMetadataService | None = None,
        context_chunk_planning_service: ContextChunkPlanningService | None = None,
        planned_chunk_builder: ContextPlannedChunkBuilder | None = None,
        fallback_chunk_builder: DeterministicFallbackChunkBuilder | None = None,
        search_chunk_builder: SearchChunkBuilder | None = None,  # search chunk 분할 담당
        embedding_service: EmbeddingService | None = None,  # 벡터 임베딩 생성 담당
    ) -> None:
        self._repository = repository
        self._transcription_service = transcription_service
        self._chunk_metadata_service = chunk_metadata_service
        self._context_chunk_planning_service = context_chunk_planning_service
        self._planned_chunk_builder = planned_chunk_builder or ContextPlannedChunkBuilder()
        self._fallback_chunk_builder = fallback_chunk_builder or DeterministicFallbackChunkBuilder(
            planned_chunk_builder=self._planned_chunk_builder
        )
        # search chunk 생성 및 embedding 서비스는 기본값으로 독립 인스턴스화 가능
        # SearchChunkBuilder에 MorphemeService를 주입하여 FTS용 형태소 텍스트 생성 활성화
        # search_chunk_builder가 외부에서 주입된 경우 그대로 사용 (테스트 편의성 보존)
        if search_chunk_builder is not None:
            self._search_chunk_builder = search_chunk_builder
        else:
            # 1. MorphemeService 인스턴스 생성 (MeCab 미설치 시 graceful fallback 내장)
            morpheme_service = MorphemeService()
            # 2. MorphemeService를 주입한 SearchChunkBuilder 생성
            self._search_chunk_builder = SearchChunkBuilder(morpheme_service=morpheme_service)
        self._embedding_service = embedding_service or EmbeddingService()

    # Create the transcript source row, run STT, and persist the reusable segments.
    async def ingest_upload(
        self,
        file: UploadFile,
        file_uri: str,
        file_name: str,
        user_id: UUID | None = None,
        folder_id: UUID | None = None,
    ) -> TranscriptIngestionResult:
        title = Path(file_name).stem

        transcript_id = await self._repository.create_transcript(
            TranscriptCreate(
                user_id=user_id,
                folder_id=folder_id,
                title=title,
                source_audio_uri=file_uri,
                original_filename=file_name,
                mime_type=file.content_type,
                status="processing",
            )
        )

        try:
            _t0_total = time.perf_counter()

            _t0 = time.perf_counter()
            transcription_service = self._transcription_service or TranscriptionService()
            transcription = await transcription_service.transcribe_with_segments(
                file
            )
            logger.info(
                "[timing] STT: %.2fs  (duration=%.1fs, model=%s)",
                time.perf_counter() - _t0,
                transcription.duration_seconds or 0,
                transcription.stt_model,
            )

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

            _t0 = time.perf_counter()
            chunks = await self._run_pipeline(transcript_id, segments)
            
            # 3. 모든 파이프라인(STT, 청킹, 인덱싱)이 완료되었으므로 세부 상태를 completed로 갱신한다.
            if user_id:
                from schemas.rag import TranscriptProcessingStatusUpdate
                await self._repository.update_processing_status(
                    transcript_id,
                    user_id,
                    TranscriptProcessingStatusUpdate(
                        status="completed",
                        content_status="completed",
                        index_status="completed",
                    )
                )

            logger.info(
                "[timing] pipeline total: %.2fs  (segments=%d, chunks=%d)",
                time.perf_counter() - _t0,
                len(segments),
                len(chunks),
            )
            logger.info(
                "[timing] ingest_upload total: %.2fs",
                time.perf_counter() - _t0_total,
            )
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
            processing_seconds=round(time.perf_counter() - _t0_total, 2),
        )

    # 실시간 전사 세그먼트(dict 목록)를 받아 SegmentCreate로 변환 후 ingest_from_segments에 위임한다.
    # route에서 pydantic 모델을 dict로 직렬화해 전달하므로 이 메서드가 변환을 담당한다.
    async def ingest_realtime_segments(
        self,
        title: str,
        duration_seconds: float,
        segments: list[dict],
        user_id: UUID | None = None,
    ) -> TranscriptIngestionResult:
        """
        실시간 전사 세그먼트를 DB에 저장하고 검색 청크를 생성합니다.

        별도 메서드를 사용하는 이유:
        - 실시간 전사에서는 STT 호출이 이미 Deepgram에서 완료됨
        - route가 dict를 전달하므로 SegmentCreate 변환을 한 곳에서 처리
        """
        segment_creates = [
            SegmentCreate(
                segment_index=s["segment_index"],
                start_seconds=s["start_seconds"],
                end_seconds=s["end_seconds"],
                text=s["text"],
                source_type="audio",
                source_start_seconds=s["start_seconds"],
                source_end_seconds=s["end_seconds"],
                raw_metadata={"source": "realtime"},
            )
            for s in segments
        ]
        return await self.ingest_from_segments(
            segments=segment_creates,
            title=title,
            duration_seconds=duration_seconds,
            user_id=user_id,
            source_uri="realtime://recording",
            source_type="audio",
            mime_type="audio/webm",
        )

    # 실시간 녹음 세션에서 클라이언트가 전달한 segments로 transcript를 저장한다.
    # STT 단계를 건너뛰고 기존 청킹/임베딩 파이프라인을 재사용한다.
    async def ingest_from_segments(
        self,
        segments: list[SegmentCreate],
        title: str | None = None,
        duration_seconds: float | None = None,
        user_id: UUID | None = None,
        folder_id: UUID | None = None,
        source_uri: str = "realtime://recording",
        original_filename: str | None = None,
        mime_type: str | None = None,
        source_type: str | None = None,
    ) -> TranscriptIngestionResult:
        _t0_total = time.perf_counter()
        settings = get_settings()
        stt_model = settings.openai_stt_model if source_type == "audio" else None
        transcript_id = await self._repository.create_transcript(
            TranscriptCreate(
                user_id=user_id,
                folder_id=folder_id,
                title=title,
                source_audio_uri=source_uri,
                original_filename=original_filename,
                mime_type=mime_type,
                duration_seconds=duration_seconds,
                stt_model=stt_model,
                status="processing",
            )
        )

        try:
            full_text = " ".join(s.text for s in segments)
            await self._repository.update_transcript_result(
                transcript_id,
                TranscriptResultUpdate(
                    full_text=full_text,
                    duration_seconds=duration_seconds,
                    stt_model=stt_model,
                    status="completed",
                ),
            )
            chunks = await self._run_pipeline(transcript_id, segments)

            # 모든 파이프라인(청킹, 인덱싱)이 완료되었으므로 세부 상태를 completed로 갱신한다.
            if user_id:
                from schemas.rag import TranscriptProcessingStatusUpdate
                await self._repository.update_processing_status(
                    transcript_id,
                    user_id,
                    TranscriptProcessingStatusUpdate(
                        status="completed",
                        content_status="completed",
                        index_status="completed",
                    )
                )
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
            transcript=full_text,
            duration_seconds=duration_seconds,
            stt_model=stt_model,
            segment_count=len(segments),
            chunk_count=len(chunks),
            processing_seconds=round(time.perf_counter() - _t0_total, 2),
        )

    async def process_existing_transcript_segments(
        self,
        transcript_id: UUID,
        segments: list[SegmentCreate],
        full_text: str,
        duration_seconds: float | None = None,
        stt_model: str | None = None,
    ) -> TranscriptIngestionResult:
        """
        기능 요약: 이미 생성된 transcript row에 공식 텍스트와 검색 인덱스를 붙인다.

        기능 흐름:
            1. transcripts.full_text/status를 갱신한다.
            2. 전달받은 segments를 기준으로 segment/chunk를 저장한다.
            3. search_chunks와 embedding까지 생성해 RAG 검색 가능 상태로 만든다.

        파라미터:
            transcript_id: 업로드 시 미리 생성된 transcript UUID.
            segments: 문서 추출/STT/임시 segment 승격 결과.
            full_text: segments를 합친 공식 원문.
        """
        _t0_total = time.perf_counter()
        await self._repository.update_transcript_result(
            transcript_id,
            TranscriptResultUpdate(
                full_text=full_text,
                duration_seconds=duration_seconds,
                stt_model=stt_model,
                status="processing",
            ),
        )
        chunks = await self._run_pipeline(
            transcript_id,
            segments,
            raise_index_errors=True,
        )
        return TranscriptIngestionResult(
            transcript_id=transcript_id,
            transcript=full_text,
            duration_seconds=duration_seconds,
            stt_model=stt_model or "",
            segment_count=len(segments),
            chunk_count=len(chunks),
            processing_seconds=round(time.perf_counter() - _t0_total, 2),
        )

    async def build_index_for_segments(
        self,
        transcript_id: UUID,
        segments: list[SegmentCreate],
        cancellation_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> list[ChunkCreate]:
        """
        기능 요약: 이미 텍스트화가 끝난 transcript의 segment/chunk/search index를 생성한다.

        기능 흐름:
            1. 전달받은 segments를 공식 segments 테이블에 저장한다.
            2. chunk 생성과 metadata enrichment를 수행한다.
            3. search_chunks와 embedding을 저장하며 실패 시 호출자에게 예외를 전달한다.

        파라미터:
            transcript_id: 업로드 시 생성된 transcript UUID.
            segments: 문서 추출/STT/임시 segment 승격 결과.
        """
        return await self._run_pipeline(
            transcript_id,
            segments,
            raise_index_errors=True,
            cancellation_checker=cancellation_checker,
        )

    # ingest_upload과 ingest_from_segments의 공통 후처리를 담당한다.
    # segments 저장 → 청크 생성/enrichment/저장 → search chunk 인덱싱 순서로 실행된다.
    async def _run_pipeline(
        self,
        transcript_id: UUID,
        segments: list[SegmentCreate],
        raise_index_errors: bool = False,
        cancellation_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> list[ChunkCreate]:
        await raise_if_cancel_requested(cancellation_checker)

        _t = time.perf_counter()
        await self._repository.insert_segments(transcript_id, segments)
        logger.info("[timing]   insert_segments: %.2fs", time.perf_counter() - _t)
        await raise_if_cancel_requested(cancellation_checker)

        _t = time.perf_counter()
        chunks = await self._build_chunks(segments, cancellation_checker)
        logger.info(
            "[timing]   chunk_planning: %.2fs  (chunks=%d)",
            time.perf_counter() - _t,
            len(chunks),
        )
        await raise_if_cancel_requested(cancellation_checker)

        _t = time.perf_counter()
        chunks = await self._enrich_chunks(chunks, cancellation_checker)
        logger.info(
            "[timing]   chunk_metadata: %.2fs  (chunks=%d, concurrency=%d)",
            time.perf_counter() - _t,
            len(chunks),
            self._get_summary_concurrency(),
        )
        await raise_if_cancel_requested(cancellation_checker)

        _t = time.perf_counter()
        await self._repository.insert_chunks(transcript_id, chunks)
        logger.info("[timing]   insert_chunks: %.2fs", time.perf_counter() - _t)
        await raise_if_cancel_requested(cancellation_checker)

        _t = time.perf_counter()
        # search chunks indexing — 실패해도 transcript는 completed 상태 유지
        await self._build_and_index_search_chunks(
            transcript_id,
            segments,
            raise_on_failure=raise_index_errors,
            cancellation_checker=cancellation_checker,
        )
        logger.info("[timing]   search_index: %.2fs", time.perf_counter() - _t)
        await raise_if_cancel_requested(cancellation_checker)

        return chunks

    def _get_summary_concurrency(self) -> int:
        from settings import get_settings
        return get_settings().summary_concurrency

    # LLM planner로 맥락 경계를 먼저 정하고, plan을 chunks 테이블 입력 모델로 변환합니다.
    # planner 초기화나 호출, plan 변환이 실패하면 deterministic fallback chunk를 저장합니다.
    async def _build_chunks(
        self,
        segments: list[SegmentCreate],
        cancellation_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> list[ChunkCreate]:
        try:
            await raise_if_cancel_requested(cancellation_checker)
            planning_service = (
                self._context_chunk_planning_service or ContextChunkPlanningService()
            )
            plan_groups = await planning_service.plan_chunks(segments)
            await raise_if_cancel_requested(cancellation_checker)
            return self._planned_chunk_builder.build(segments, plan_groups)
        except Exception:
            await raise_if_cancel_requested(cancellation_checker)
            return self._fallback_chunk_builder.build(segments)

    # chunk metadata를 생성합니다.
    # OpenAI 설정이 없거나 생성 중 오류가 나면 원본 chunk를 반환해 transcript 저장 흐름을 유지합니다.
    async def _enrich_chunks(
        self,
        chunks: list[ChunkCreate],
        cancellation_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> list[ChunkCreate]:
        try:
            await raise_if_cancel_requested(cancellation_checker)
            metadata_service = self._chunk_metadata_service or ChunkMetadataService()
            try:
                return await metadata_service.enrich_chunks(
                    chunks,
                    cancellation_checker=cancellation_checker,
                )
            except TypeError as exc:
                if "cancellation_checker" not in str(exc):
                    raise
                return await metadata_service.enrich_chunks(chunks)
        except Exception:
            await raise_if_cancel_requested(cancellation_checker)
            return chunks

    # parent chunks를 vector search용 child search unit으로 분할하고 embedding을 생성해 저장한다.
    # embedding/search chunk 단계 실패는 transcript 완료 상태에 영향을 주지 않는다.
    # 이 메서드가 필요한 이유:
    #   ingest_upload의 핵심 흐름(STT → segments → chunks)과 search indexing을 분리해
    #   search indexing 실패가 전체 파이프라인을 중단시키지 않도록 책임을 격리한다.
    async def _build_and_index_search_chunks(
        self,
        transcript_id: UUID,
        segments: list[SegmentCreate],
        raise_on_failure: bool = False,
        cancellation_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """
        기능 요약: parent chunks를 child search unit으로 분할하고 embedding을 생성해 저장한다.

        기능 흐름:
            1. DB에서 parent chunks 조회 (chunk_index 순서, parent_chunk_id 확보 목적)
            2. SearchChunkBuilder로 adaptive grouping해 child search chunks 생성
            3. search chunks 텍스트 목록을 EmbeddingService로 배치 embedding 생성
            4. embedding을 각 search chunk에 추가하고 embedding_model 설정
            5. RagRepository로 bulk upsert

        파라미터:
            transcript_id: 이 transcript의 UUID (예: UUID("a1b2c3..."))
            segments: transcript의 모든 SegmentCreate 목록 (search chunk 텍스트 조합에 사용)
        """
        try:
            # 1. DB에서 parent chunks 조회 — insert_chunks() 완료 후 실제 UUID(parent_chunk_id) 확보
            await raise_if_cancel_requested(cancellation_checker)
            parent_chunks = await self._repository.fetch_chunks_by_transcript(transcript_id)
            if not parent_chunks:
                # parent chunk가 없으면 search chunk도 생성 불필요
                return

            # 2. adaptive grouping으로 child search chunks 생성
            await raise_if_cancel_requested(cancellation_checker)
            search_chunks = self._search_chunk_builder.build(parent_chunks, segments)
            if not search_chunks:
                # 묶을 segment가 없으면 저장 불필요
                return

            # 3. search chunk 텍스트 배열 추출 후 배치 embedding 생성
            await raise_if_cancel_requested(cancellation_checker)
            texts = [chunk.text for chunk in search_chunks]
            embeddings = await self._embedding_service.embed(texts)
            await raise_if_cancel_requested(cancellation_checker)

            # 4. embedding을 search chunks에 추가 (SearchChunkCreate는 frozen이므로 새 객체 생성)
            search_chunks_with_embeddings = [
                SearchChunkCreate(
                    parent_chunk_id=search_chunks[i].parent_chunk_id,
                    child_index=search_chunks[i].child_index,
                    segment_start_index=search_chunks[i].segment_start_index,
                    segment_end_index=search_chunks[i].segment_end_index,
                    start_seconds=search_chunks[i].start_seconds,
                    end_seconds=search_chunks[i].end_seconds,
                    text=search_chunks[i].text,
                    text_morphemes=search_chunks[i].text_morphemes,
                    metadata=search_chunks[i].metadata,
                    # EmbeddingService 기본 모델값과 동기화
                    embedding_model="text-embedding-3-small",
                    embedding=embeddings[i],
                    source_type=search_chunks[i].source_type,
                    source_page_start=search_chunks[i].source_page_start,
                    source_page_end=search_chunks[i].source_page_end,
                    source_slide_start=search_chunks[i].source_slide_start,
                    source_slide_end=search_chunks[i].source_slide_end,
                    source_start_seconds=search_chunks[i].source_start_seconds,
                    source_end_seconds=search_chunks[i].source_end_seconds,
                )
                for i in range(len(search_chunks))
            ]

            # 5. search chunks bulk upsert
            await self._repository.insert_search_chunks(
                transcript_id,
                search_chunks_with_embeddings,
            )
            await raise_if_cancel_requested(cancellation_checker)

        except Exception as exc:
            if raise_on_failure:
                raise
            # embedding/search chunk 단계 실패는 transcript 실패로 전파하지 않음
            logger.error(
                "Search chunk indexing failed for transcript %s: %s",
                transcript_id,
                exc,
            )

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
                    source_type="audio",
                    source_start_seconds=start_seconds,
                    source_end_seconds=end_seconds,
                )
            )

        return segment_creates
