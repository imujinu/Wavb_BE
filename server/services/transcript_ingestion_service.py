import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, UploadFile

from repositories.rag_repository import RagRepository
from schemas.rag import (
    ChunkCreate,
    DomainType,
    SearchChunkCreate,
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
from services.embedding_service import EmbeddingService
from services.search_chunk_builder import SearchChunkBuilder
from services.transcription_service import TranscriptionService, TranscriptionSegment

logger = logging.getLogger(__name__)


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
        search_chunk_builder: SearchChunkBuilder | None = None,  # search chunk 분할 담당
        embedding_service: EmbeddingService | None = None,  # 벡터 임베딩 생성 담당
    ) -> None:
        self._repository = repository
        self._transcription_service = transcription_service or TranscriptionService()
        self._chunk_metadata_service = chunk_metadata_service
        self._context_chunk_planning_service = context_chunk_planning_service
        self._planned_chunk_builder = planned_chunk_builder or ContextPlannedChunkBuilder()
        self._fallback_chunk_builder = fallback_chunk_builder or DeterministicFallbackChunkBuilder(
            planned_chunk_builder=self._planned_chunk_builder
        )
        # search chunk 생성 및 embedding 서비스는 기본값으로 독립 인스턴스화 가능
        self._search_chunk_builder = search_chunk_builder or SearchChunkBuilder()
        self._embedding_service = embedding_service or EmbeddingService()

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
            # search chunks indexing — 실패해도 transcript는 completed 상태 유지
            await self._build_and_index_search_chunks(transcript_id, segments)
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

    # parent chunks를 vector search용 child search unit으로 분할하고 embedding을 생성해 저장한다.
    # embedding/search chunk 단계 실패는 transcript 완료 상태에 영향을 주지 않는다.
    # 이 메서드가 필요한 이유:
    #   ingest_upload의 핵심 흐름(STT → segments → chunks)과 search indexing을 분리해
    #   search indexing 실패가 전체 파이프라인을 중단시키지 않도록 책임을 격리한다.
    async def _build_and_index_search_chunks(
        self,
        transcript_id: UUID,
        segments: list[SegmentCreate],
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
            parent_chunks = await self._repository.fetch_chunks_by_transcript(transcript_id)
            if not parent_chunks:
                # parent chunk가 없으면 search chunk도 생성 불필요
                return

            # 2. adaptive grouping으로 child search chunks 생성
            search_chunks = self._search_chunk_builder.build(parent_chunks, segments)
            if not search_chunks:
                # 묶을 segment가 없으면 저장 불필요
                return

            # 3. search chunk 텍스트 배열 추출 후 배치 embedding 생성
            texts = [chunk.text for chunk in search_chunks]
            embeddings = await self._embedding_service.embed(texts)

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
                    metadata=search_chunks[i].metadata,
                    # EmbeddingService 기본 모델값과 동기화
                    embedding_model="text-embedding-3-small",
                    embedding=embeddings[i],
                )
                for i in range(len(search_chunks))
            ]

            # 5. search chunks bulk upsert
            await self._repository.insert_search_chunks(
                transcript_id,
                search_chunks_with_embeddings,
            )

        except Exception as exc:
            # embedding/search chunk 단계 실패는 transcript 실패로 전파하지 않음
            logger.error(
                "Search chunk indexing failed for transcript %s: %s",
                transcript_id,
                exc,
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
