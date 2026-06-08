import json
from typing import Any
from uuid import UUID, uuid4

from db.connection import DatabaseConnection
from schemas.rag import (
    ChunkCreate,
    ChunkRow,
    FileDetail,
    LectureSummaryCreate,
    LectureSummaryDetail,
    ParentChunkResult,
    SearchChunkCreate,
    SearchChunkHit,
    SegmentCreate,
    SummaryDocumentCreate,
    SummaryDocumentDetail,
    TemporarySegmentCreate,
    TemporarySegmentDetail,
    TranscriptCreate,
    TranscriptDetail,
    TranscriptProcessingDetail,
    TranscriptProcessingStatusUpdate,
    TranscriptResultUpdate,
    UploadedFileDetail,
)


class RagRepository:
    def __init__(self, connection: DatabaseConnection) -> None:
        self._connection = connection

    # transcript source-of-truth row를 만들고 이후 STT/segment/chunk 저장의 기준 id를 반환한다.
    async def create_transcript(self, transcript: TranscriptCreate) -> UUID:
        transcript_id = uuid4()
        row = await self._connection.fetchrow(
            """
            INSERT INTO transcripts (
              id, user_id, title, source_audio_uri,
              original_filename, mime_type, duration_seconds, language,
              stt_model, status, folder_id, source_type,
              content_status, index_status, temporary_text
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            RETURNING id
            """,
            transcript_id,
            transcript.user_id,
            transcript.title,
            transcript.source_audio_uri,
            transcript.original_filename,
            transcript.mime_type,
            transcript.duration_seconds,
            transcript.language,
            transcript.stt_model,
            transcript.status,
            transcript.folder_id,
            transcript.source_type,
            transcript.content_status,
            transcript.index_status,
            transcript.temporary_text,
        )
        return row["id"] if row else transcript_id

    # STT 완료/실패 결과를 transcripts에 반영해서 현재 처리 상태를 영속화한다.
    async def update_transcript_result(
        self,
        transcript_id: UUID,
        update: TranscriptResultUpdate,
    ) -> None:
        await self._connection.execute(
            """
            UPDATE transcripts
            SET full_text = $2,
                summary = $3,
                duration_seconds = COALESCE($4, duration_seconds),
                stt_model = COALESCE($5, stt_model),
                status = $6,
                error_message = $7,
                updated_at = now()
            WHERE id = $1
            """,
            transcript_id,
            update.full_text,
            update.summary,
            update.duration_seconds,
            update.stt_model,
            update.status,
            update.error_message,
        )

    # 저장된 transcript를 id로 단건 조회해 요약 PDF 생성의 입력(full_text + 메타)을 제공한다.
    # update_transcript_result()가 쓰기만 하던 full_text를 읽는 경로를 보완한다.
    async def get_transcript_by_id(
        self,
        transcript_id: UUID,
        user_id: UUID | None = None,
    ) -> TranscriptDetail | None:
        """
        기능 요약: transcripts에서 id로 1건 조회한다. user_id가 주어지면 소유권까지 필터한다.

        기능 흐름:
            1. user_id 유무에 따라 소유권 필터(AND user_id = $2)를 동적으로 추가
            2. fetchrow로 단건 조회 — 없으면 None 반환
            3. TranscriptDetail 읽기 전용 모델로 매핑하여 반환

        파라미터:
            transcript_id: 조회할 transcript UUID (예: UUID("a1b2c3..."))
            user_id: 소유권 검증용 사용자 UUID. None이면 소유권 필터 없이 조회 (예: 내부 배치)
        """
        # 1. user_id가 주어지면 타인 transcript 접근을 차단하는 소유권 필터를 추가
        if user_id is not None:
            row = await self._connection.fetchrow(
                """
                SELECT id, user_id, title, full_text, summary,
                       duration_seconds, language, status, created_at
                FROM transcripts
                WHERE id = $1 AND user_id = $2
                """,
                transcript_id,
                user_id,
            )
        else:
            row = await self._connection.fetchrow(
                """
                SELECT id, user_id, title, full_text, summary,
                       duration_seconds, language, status, created_at
                FROM transcripts
                WHERE id = $1
                """,
                transcript_id,
            )

        # 2. 조회 결과가 없으면 None — 라우트에서 404로 변환
        if row is None:
            return None

        # 3. 읽기 전용 모델로 매핑 (NUMERIC duration_seconds는 float로 정규화)
        return TranscriptDetail(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            full_text=row["full_text"],
            summary=row["summary"],
            duration_seconds=(
                float(row["duration_seconds"])
                if row["duration_seconds"] is not None
                else None
            ),
            language=row["language"],
            status=row["status"],
            created_at=row["created_at"],
        )

    # 인증 사용자가 업로드한 원본 파일 목록을 최신순으로 조회한다.
    async def get_file_detail_by_id(
        self,
        transcript_id: UUID,
        user_id: UUID,
    ) -> FileDetail | None:
        row = await self._connection.fetchrow(
            """
            SELECT id, title, source_audio_uri, original_filename, mime_type,
                   source_type, status, content_status, index_status,
                   error_message, duration_seconds, created_at, updated_at
            FROM transcripts
            WHERE id = $1 AND user_id = $2
            """,
            transcript_id,
            user_id,
        )
        if row is None:
            return None

        return FileDetail(
            transcript_id=row["id"],
            title=row["title"],
            file_uri=row["source_audio_uri"],
            original_filename=row["original_filename"],
            mime_type=row["mime_type"],
            source_type=row["source_type"],
            status=row["status"],
            content_status=row["content_status"],
            index_status=row["index_status"],
            error_message=row["error_message"],
            duration_seconds=(
                float(row["duration_seconds"])
                if row["duration_seconds"] is not None
                else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_transcripts_by_user(
        self,
        user_id: UUID,
    ) -> list[UploadedFileDetail]:
        """
        기능 요약: 사용자별 저장 파일 목록 화면에 필요한 transcript 메타데이터를 조회한다.

        기능 흐름:
            1. transcripts.user_id로 소유 파일만 필터링한다.
            2. created_at DESC로 최신 업로드부터 정렬한다.
            3. source_audio_uri를 API 응답의 file_uri로 매핑한다.

        파라미터:
            user_id: 인증 사용자 UUID (예: UUID("aaaaaaaa-..."))
        """
        rows = await self._connection.fetch(
            """
            SELECT id, title, source_audio_uri, original_filename,
                   mime_type, status, created_at
            FROM transcripts
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )

        return [
            UploadedFileDetail(
                transcript_id=row["id"],
                title=row["title"],
                file_uri=row["source_audio_uri"],
                original_filename=row["original_filename"],
                mime_type=row["mime_type"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # 처리 API에서 사용할 transcript 원본/상태 정보를 소유권 기준으로 조회한다.
    async def get_transcript_for_processing(
        self,
        transcript_id: UUID,
        user_id: UUID,
    ) -> TranscriptProcessingDetail | None:
        """
        기능 요약: 지연 처리 API가 필요한 transcript 메타데이터와 처리 상태를 조회한다.

        기능 흐름:
            1. transcript_id와 user_id를 함께 조건으로 걸어 타인 파일 접근을 차단한다.
            2. 원본 URI, 파일명, source_type, content/index 상태를 함께 읽는다.
            3. 처리 서비스가 사용할 읽기 전용 모델로 매핑한다.

        파라미터:
            transcript_id: 처리할 transcript UUID.
            user_id: 인증 사용자 UUID.
        """
        row = await self._connection.fetchrow(
            """
            SELECT id, user_id, title, source_audio_uri, original_filename,
                   mime_type, duration_seconds, stt_model, full_text, status,
                   source_type, content_status, index_status, temporary_text,
                   error_message
            FROM transcripts
            WHERE id = $1 AND user_id = $2
            """,
            transcript_id,
            user_id,
        )
        if row is None:
            return None

        return TranscriptProcessingDetail(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            source_audio_uri=row["source_audio_uri"],
            original_filename=row["original_filename"],
            mime_type=row["mime_type"],
            duration_seconds=(
                float(row["duration_seconds"])
                if row["duration_seconds"] is not None
                else None
            ),
            stt_model=row["stt_model"],
            full_text=row["full_text"],
            status=row["status"],
            source_type=row["source_type"],
            content_status=row["content_status"],
            index_status=row["index_status"],
            temporary_text=row["temporary_text"],
            error_message=row["error_message"],
        )

    # content/index 단계 상태를 별도로 갱신해 업로드 목록용 대표 status와 내부 상태를 함께 유지한다.
    async def update_processing_status(
        self,
        transcript_id: UUID,
        user_id: UUID,
        update: TranscriptProcessingStatusUpdate,
    ) -> bool:
        row = await self._connection.fetchrow(
            """
            UPDATE transcripts
            SET status = COALESCE($3, status),
                content_status = COALESCE($4, content_status),
                index_status = COALESCE($5, index_status),
                error_message = $6,
                processed_at = CASE
                  WHEN $4 = 'completed' THEN now()
                  ELSE processed_at
                END,
                indexed_at = CASE
                  WHEN $5 = 'completed' THEN now()
                  ELSE indexed_at
                END,
                cancelled_at = CASE
                  WHEN $3 = 'cancelled' THEN now()
                  ELSE cancelled_at
                END,
                updated_at = now()
            WHERE id = $1 AND user_id = $2
            RETURNING id
            """,
            transcript_id,
            user_id,
            update.status,
            update.content_status,
            update.index_status,
            update.error_message,
        )
        return row is not None

    async def request_processing_cancel(
        self,
        transcript_id: UUID,
        user_id: UUID,
    ) -> bool:
        row = await self._connection.fetchrow(
            """
            UPDATE transcripts
            SET cancel_requested_at = COALESCE(cancel_requested_at, now()),
                status = CASE
                  WHEN status IN ('completed', 'failed', 'cancelled') THEN status
                  WHEN status = 'processing' THEN 'cancel_requested'
                  ELSE 'cancelled'
                END,
                content_status = CASE
                  WHEN status = 'processing' AND content_status = 'processing'
                    THEN 'cancel_requested'
                  WHEN status <> 'processing'
                       AND content_status IN ('pending', 'processing', 'cancel_requested')
                    THEN 'cancelled'
                  ELSE content_status
                END,
                index_status = CASE
                  WHEN status = 'processing' AND index_status = 'processing'
                    THEN 'cancel_requested'
                  WHEN status <> 'processing'
                       AND index_status IN ('pending', 'processing', 'cancel_requested')
                    THEN 'cancelled'
                  ELSE index_status
                END,
                cancelled_at = CASE
                  WHEN status = 'processing'
                       OR status IN ('completed', 'failed', 'cancelled')
                    THEN cancelled_at
                  ELSE COALESCE(cancelled_at, now())
                END,
                updated_at = now()
            WHERE id = $1 AND user_id = $2
            RETURNING id
            """,
            transcript_id,
            user_id,
        )
        return row is not None

    async def is_processing_cancel_requested(
        self,
        transcript_id: UUID,
        user_id: UUID,
    ) -> bool:
        row = await self._connection.fetchrow(
            """
            SELECT id
            FROM transcripts
            WHERE id = $1
              AND user_id = $2
              AND cancel_requested_at IS NOT NULL
              AND status IN ('processing', 'cancel_requested', 'cancelled')
            """,
            transcript_id,
            user_id,
        )
        return row is not None

    async def is_processing_cancel_requested_after(
        self,
        transcript_id: UUID,
        user_id: UUID,
        started_at,
    ) -> bool:
        row = await self._connection.fetchrow(
            """
            SELECT id
            FROM transcripts
            WHERE id = $1
              AND user_id = $2
              AND cancel_requested_at IS NOT NULL
              AND cancel_requested_at >= $3
            """,
            transcript_id,
            user_id,
            started_at,
        )
        return row is not None

    # 실시간 STT final 이벤트를 임시 segment 테이블에 append/upsert한다.
    async def insert_temporary_segment(
        self,
        transcript_id: UUID,
        segment: TemporarySegmentCreate,
    ) -> None:
        await self._connection.execute(
            """
            INSERT INTO temporary_segments (
              id, transcript_id, segment_index, start_seconds,
              end_seconds, text, raw_metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (transcript_id, segment_index) DO UPDATE
            SET start_seconds = EXCLUDED.start_seconds,
                end_seconds = EXCLUDED.end_seconds,
                text = EXCLUDED.text,
                raw_metadata = EXCLUDED.raw_metadata
            """,
            uuid4(),
            transcript_id,
            segment.segment_index,
            segment.start_seconds,
            segment.end_seconds,
            segment.text,
            self._to_json(segment.raw_metadata),
        )

    async def list_temporary_segments(
        self,
        transcript_id: UUID,
    ) -> list[TemporarySegmentDetail]:
        rows = await self._connection.fetch(
            """
            SELECT id, transcript_id, segment_index, start_seconds,
                   end_seconds, text, raw_metadata
            FROM temporary_segments
            WHERE transcript_id = $1
            ORDER BY segment_index
            """,
            transcript_id,
        )
        return [
            TemporarySegmentDetail(
                id=row["id"],
                transcript_id=row["transcript_id"],
                segment_index=row["segment_index"],
                start_seconds=(
                    float(row["start_seconds"])
                    if row["start_seconds"] is not None
                    else None
                ),
                end_seconds=(
                    float(row["end_seconds"])
                    if row["end_seconds"] is not None
                    else None
                ),
                text=row["text"],
                raw_metadata=self._to_dict(row["raw_metadata"]),
            )
            for row in rows
        ]

    async def update_temporary_text(
        self,
        transcript_id: UUID,
        temporary_text: str,
    ) -> None:
        await self._connection.execute(
            """
            UPDATE transcripts
            SET temporary_text = $2, updated_at = now()
            WHERE id = $1
            """,
            transcript_id,
            temporary_text,
        )

    async def fetch_segments_by_transcript(
        self,
        transcript_id: UUID,
    ) -> list[SegmentCreate]:
        rows = await self._connection.fetch(
            """
            SELECT segment_index, speaker_label, start_seconds, end_seconds,
                   text, confidence, raw_metadata, source_type,
                   source_page_start, source_page_end,
                   source_slide_start, source_slide_end,
                   source_start_seconds, source_end_seconds
            FROM segments
            WHERE transcript_id = $1
            ORDER BY segment_index
            """,
            transcript_id,
        )
        return [
            SegmentCreate(
                segment_index=row["segment_index"],
                speaker_label=row["speaker_label"],
                start_seconds=float(row["start_seconds"]),
                end_seconds=float(row["end_seconds"]),
                text=row["text"],
                confidence=(
                    float(row["confidence"])
                    if row["confidence"] is not None
                    else None
                ),
                raw_metadata=self._to_dict(row["raw_metadata"]),
                source_type=self._row_value(row, "source_type"),
                source_page_start=self._row_value(row, "source_page_start"),
                source_page_end=self._row_value(row, "source_page_end"),
                source_slide_start=self._row_value(row, "source_slide_start"),
                source_slide_end=self._row_value(row, "source_slide_end"),
                source_start_seconds=(
                    float(self._row_value(row, "source_start_seconds"))
                    if self._row_value(row, "source_start_seconds") is not None
                    else None
                ),
                source_end_seconds=(
                    float(self._row_value(row, "source_end_seconds"))
                    if self._row_value(row, "source_end_seconds") is not None
                    else None
                ),
            )
            for row in rows
        ]

    async def count_segments_by_transcript(self, transcript_id: UUID) -> int:
        row = await self._connection.fetchrow(
            "SELECT COUNT(*) AS count FROM segments WHERE transcript_id = $1",
            transcript_id,
        )
        return int(row["count"]) if row else 0

    async def count_chunks_by_transcript(self, transcript_id: UUID) -> int:
        row = await self._connection.fetchrow(
            "SELECT COUNT(*) AS count FROM chunks WHERE transcript_id = $1",
            transcript_id,
        )
        return int(row["count"]) if row else 0

    # STT 최소 단위 segment를 저장해서 playback, 재chunking, speaker 검색의 기준으로 사용한다.
    async def insert_segments(
        self,
        transcript_id: UUID,
        segments: list[SegmentCreate],
    ) -> None:
        if not segments:
            return

        await self._connection.executemany(
            """
            INSERT INTO segments (
              id, transcript_id, segment_index, speaker_label,
              start_seconds, end_seconds, text, confidence, raw_metadata,
              source_type, source_page_start, source_page_end,
              source_slide_start, source_slide_end,
              source_start_seconds, source_end_seconds
            )
            VALUES (
              $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb,
              $10, $11, $12, $13, $14, $15, $16
            )
            ON CONFLICT (transcript_id, segment_index) DO UPDATE
            SET speaker_label = EXCLUDED.speaker_label,
                start_seconds = EXCLUDED.start_seconds,
                end_seconds = EXCLUDED.end_seconds,
                text = EXCLUDED.text,
                confidence = EXCLUDED.confidence,
                raw_metadata = EXCLUDED.raw_metadata,
                source_type = EXCLUDED.source_type,
                source_page_start = EXCLUDED.source_page_start,
                source_page_end = EXCLUDED.source_page_end,
                source_slide_start = EXCLUDED.source_slide_start,
                source_slide_end = EXCLUDED.source_slide_end,
                source_start_seconds = EXCLUDED.source_start_seconds,
                source_end_seconds = EXCLUDED.source_end_seconds
            """,
            [
                (
                    uuid4(),
                    transcript_id,
                    segment.segment_index,
                    segment.speaker_label,
                    segment.start_seconds,
                    segment.end_seconds,
                    segment.text,
                    segment.confidence,
                    self._to_json(segment.raw_metadata),
                    segment.source_type,
                    segment.source_page_start,
                    segment.source_page_end,
                    segment.source_slide_start,
                    segment.source_slide_end,
                    segment.source_start_seconds,
                    segment.source_end_seconds,
                )
                for segment in segments
            ],
        )

    # retrieval 전용 chunk를 저장해서 metadata/full-text/vector 검색의 입력으로 사용한다.
    async def insert_chunks(
        self,
        transcript_id: UUID,
        chunks: list[ChunkCreate],
    ) -> None:
        if not chunks:
            return

        await self._connection.executemany(
            """
            INSERT INTO chunks (
              id, transcript_id, chunk_index, chunk_strategy,
              segment_start_index, segment_end_index, start_seconds, end_seconds,
              text, summary, topic, subtopic, keywords, speaker_labels,
              metadata, embedding_model, embedding,
              source_type, source_page_start, source_page_end,
              source_slide_start, source_slide_end,
              source_start_seconds, source_end_seconds
            )
            VALUES (
              $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
              $11, $12, $13, $14, $15::jsonb, $16, $17::vector,
              $18, $19, $20, $21, $22, $23, $24
            )
            ON CONFLICT (transcript_id, chunk_strategy, chunk_index) DO UPDATE
            SET text = EXCLUDED.text,
                segment_start_index = EXCLUDED.segment_start_index,
                segment_end_index = EXCLUDED.segment_end_index,
                start_seconds = EXCLUDED.start_seconds,
                end_seconds = EXCLUDED.end_seconds,
                summary = EXCLUDED.summary,
                topic = EXCLUDED.topic,
                subtopic = EXCLUDED.subtopic,
                keywords = EXCLUDED.keywords,
                speaker_labels = EXCLUDED.speaker_labels,
                metadata = EXCLUDED.metadata,
                embedding_model = EXCLUDED.embedding_model,
                embedding = EXCLUDED.embedding,
                source_type = EXCLUDED.source_type,
                source_page_start = EXCLUDED.source_page_start,
                source_page_end = EXCLUDED.source_page_end,
                source_slide_start = EXCLUDED.source_slide_start,
                source_slide_end = EXCLUDED.source_slide_end,
                source_start_seconds = EXCLUDED.source_start_seconds,
                source_end_seconds = EXCLUDED.source_end_seconds
            """,
            [
                (
                    uuid4(),
                    transcript_id,
                    chunk.chunk_index,
                    chunk.chunk_strategy,
                    chunk.segment_start_index,
                    chunk.segment_end_index,
                    chunk.start_seconds,
                    chunk.end_seconds,
                    chunk.text,
                    chunk.summary,
                    chunk.topic,
                    chunk.subtopic,
                    chunk.keywords,
                    chunk.speaker_labels,
                    self._to_json(chunk.metadata),
                    chunk.embedding_model,
                    self._to_vector_literal(chunk.embedding),
                    chunk.source_type,
                    chunk.source_page_start,
                    chunk.source_page_end,
                    chunk.source_slide_start,
                    chunk.source_slide_end,
                    chunk.source_start_seconds,
                    chunk.source_end_seconds,
                )
                for chunk in chunks
            ],
        )

    # insert_chunks() 완료 후 search_chunks 생성에 필요한 parent_chunk_id를 얻기 위해 조회한다.
    async def fetch_chunks_by_transcript(self, transcript_id: UUID) -> list[ChunkRow]:
        rows = await self._connection.fetch(
            """
            SELECT id, chunk_index, topic, subtopic, keywords, speaker_labels,
                   segment_start_index, segment_end_index, start_seconds,
                   end_seconds, text, summary, metadata,
                   source_type, source_page_start, source_page_end,
                   source_slide_start, source_slide_end,
                   source_start_seconds, source_end_seconds
            FROM chunks
            WHERE transcript_id = $1
            ORDER BY chunk_index
            """,
            transcript_id,
        )

        return [
            ChunkRow(
                id=row["id"],
                chunk_index=row["chunk_index"],
                topic=row["topic"],
                subtopic=row["subtopic"],
                keywords=list(row["keywords"]) if row["keywords"] else [],
                speaker_labels=list(row["speaker_labels"]) if row["speaker_labels"] else [],
                segment_start_index=row["segment_start_index"],
                segment_end_index=row["segment_end_index"],
                start_seconds=float(row["start_seconds"]) if row["start_seconds"] is not None else None,
                end_seconds=float(row["end_seconds"]) if row["end_seconds"] is not None else None,
                text=row["text"],
                summary=row["summary"],
                metadata=self._to_dict(row["metadata"]),
                source_type=self._row_value(row, "source_type"),
                source_page_start=self._row_value(row, "source_page_start"),
                source_page_end=self._row_value(row, "source_page_end"),
                source_slide_start=self._row_value(row, "source_slide_start"),
                source_slide_end=self._row_value(row, "source_slide_end"),
                source_start_seconds=(
                    float(self._row_value(row, "source_start_seconds"))
                    if self._row_value(row, "source_start_seconds") is not None
                    else None
                ),
                source_end_seconds=(
                    float(self._row_value(row, "source_end_seconds"))
                    if self._row_value(row, "source_end_seconds") is not None
                    else None
                ),
            )
            for row in rows
        ]

    # insert_chunks() 완료 후 fetch_chunks_by_transcript()로 parent_chunk_id를 확보하고,
    # SearchChunkSplitService가 생성한 search_chunks를 이 메서드로 bulk upsert한다.
    # transcript_id를 직접 저장해 transcript 단위 전체 조회/삭제 경로를 최적화한다.
    async def insert_search_chunks(
        self,
        transcript_id: UUID,
        search_chunks: list[SearchChunkCreate],
    ) -> None:
        """
        기능 요약: search_chunks를 DB에 bulk upsert한다. parent_chunk_id + child_index 기준 conflict handling.

        기능 흐름:
            1. 빈 목록은 조기 반환
            2. executemany로 일괄 insert/upsert 실행
            3. upsert 키: (parent_chunk_id, child_index)
            4. embedding은 _to_vector_literal()로 직렬화, metadata는 _to_json()으로 직렬화

        파라미터:
            transcript_id: 이 청크들이 속한 transcript의 ID (예: UUID("a1b2c3..."))
            search_chunks: 저장할 search unit 목록 (예: parent chunk를 3분할한 child list)
        """
        # 1. 빈 목록 조기 반환 — executemany에 빈 리스트를 넘기지 않도록 방어
        if not search_chunks:
            return

        # 2. search_chunks를 일괄 upsert — parent_chunk_id + child_index 충돌 시 전체 필드 갱신
        # text_morphemes($10): 형태소 분석 결과 텍스트, NULL 허용 (FTS에서 coalesce로 text fallback)
        await self._connection.executemany(
            """
            INSERT INTO search_chunks (
              id, transcript_id, parent_chunk_id, child_index,
              segment_start_index, segment_end_index, start_seconds, end_seconds,
              text, text_morphemes, embedding_model, embedding, metadata,
              source_type, source_page_start, source_page_end,
              source_slide_start, source_slide_end,
              source_start_seconds, source_end_seconds
            )
            VALUES (
              $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
              $11, $12::vector, $13::jsonb,
              $14, $15, $16, $17, $18, $19, $20
            )
            ON CONFLICT (parent_chunk_id, child_index) DO UPDATE
            SET segment_start_index = EXCLUDED.segment_start_index,
                segment_end_index = EXCLUDED.segment_end_index,
                start_seconds = EXCLUDED.start_seconds,
                end_seconds = EXCLUDED.end_seconds,
                text = EXCLUDED.text,
                text_morphemes = EXCLUDED.text_morphemes,
                embedding_model = EXCLUDED.embedding_model,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                source_type = EXCLUDED.source_type,
                source_page_start = EXCLUDED.source_page_start,
                source_page_end = EXCLUDED.source_page_end,
                source_slide_start = EXCLUDED.source_slide_start,
                source_slide_end = EXCLUDED.source_slide_end,
                source_start_seconds = EXCLUDED.source_start_seconds,
                source_end_seconds = EXCLUDED.source_end_seconds
            """,
            [
                (
                    uuid4(),
                    transcript_id,
                    chunk.parent_chunk_id,
                    chunk.child_index,
                    chunk.segment_start_index,
                    chunk.segment_end_index,
                    chunk.start_seconds,
                    chunk.end_seconds,
                    chunk.text,
                    chunk.text_morphemes,
                    chunk.embedding_model,
                    self._to_vector_literal(chunk.embedding),
                    self._to_json(chunk.metadata),
                    chunk.source_type,
                    chunk.source_page_start,
                    chunk.source_page_end,
                    chunk.source_slide_start,
                    chunk.source_slide_end,
                    chunk.source_start_seconds,
                    chunk.source_end_seconds,
                )
                for chunk in search_chunks
            ],
        )

    # keyword 검색과 vector 검색 결과를 RRF(Reciprocal Rank Fusion)로 합쳐 최종 순위를 결정한다.
    # RRF는 각 채널의 원점수(ts_rank vs cosine similarity)를 직접 더하지 않고 "순위"만 사용한다.
    # ts_rank(0.0x대)와 vector similarity(0.7~0.9대)는 스케일이 달라 가중 합산이 왜곡되는데,
    # 순위 기반 융합은 이 스케일 불일치를 원천적으로 제거해 안정적인 하이브리드 순위를 만든다.
    async def search_chunks_hybrid(
        self,
        morpheme_query: str,
        embedding: list[float],
        transcript_ids: list[UUID],
        user_id: UUID | None,
        top_k: int,
        keyword_weight: float = 0.6,
        vector_weight: float = 0.4,
        rrf_k: int = 60,
    ) -> list[SearchChunkHit]:
        """
        기능 요약: 키워드/벡터 검색을 병렬 실행한 뒤 가중 RRF로 융합해 상위 top_k hit을 반환한다.

        기능 흐름:
            1. _search_by_keyword / _search_by_vector 병렬 실행 (각 채널은 자체 점수순 정렬됨)
            2. 각 채널의 리스트 인덱스(0-based) → 순위(1-based)로 변환하여 RRF 점수 누적
               score(doc) = Σ weight_i * 1 / (rrf_k + rank_i)
               - 한쪽 채널에만 등장한 hit은 그 채널 기여분만 합산됨
            3. RRF 점수를 score 필드에 담아 SearchChunkHit 재구성
            4. RRF 점수 내림차순 정렬 후 top_k 반환

        파라미터:
            morpheme_query: 형태소 분석된 FTS 쿼리 (예: "다음 출시 일정 논의")
            embedding: 원문 쿼리 임베딩 벡터 (길이 1536)
            transcript_ids / user_id: 검색 범위 한정 필터
            top_k: 반환할 최대 hit 수
            keyword_weight / vector_weight: 채널별 RRF 기여 가중치 (0.6 / 0.4)
            rrf_k: RRF 완충 상수 — 클수록 상위·하위 순위 간 점수 차가 완만해짐 (관례값 60)
        """
        # 1. 같은 asyncpg connection에서 동시에 쿼리하면 InterfaceError가 발생하므로 순차 실행한다.
        #    각 결과는 자체 점수 기준 내림차순 정렬 상태다.
        keyword_hits = await self._search_by_keyword(
            morpheme_query,
            transcript_ids,
            user_id,
            top_k,
        )
        vector_hits = await self._search_by_vector(
            embedding,
            transcript_ids,
            user_id,
            top_k,
        )

        # 2. RRF 점수 누적 — 리스트 순서가 곧 순위이므로 enumerate 인덱스 + 1을 rank로 사용
        #    source_by_id: score 재계산 후 SearchChunkHit를 복원하기 위한 원본 hit 보관
        rrf_scores: dict[UUID, float] = {}
        source_by_id: dict[UUID, SearchChunkHit] = {}

        for rank, hit in enumerate(keyword_hits, start=1):
            rrf_scores[hit.id] = rrf_scores.get(hit.id, 0.0) + keyword_weight / (rrf_k + rank)
            source_by_id.setdefault(hit.id, hit)

        for rank, hit in enumerate(vector_hits, start=1):
            rrf_scores[hit.id] = rrf_scores.get(hit.id, 0.0) + vector_weight / (rrf_k + rank)
            source_by_id.setdefault(hit.id, hit)

        # 3. RRF 점수를 score 필드에 담아 SearchChunkHit 재구성
        merged: list[SearchChunkHit] = [
            SearchChunkHit(
                id=hit.id,
                transcript_id=hit.transcript_id,
                parent_chunk_id=hit.parent_chunk_id,
                child_index=hit.child_index,
                start_seconds=hit.start_seconds,
                end_seconds=hit.end_seconds,
                text=hit.text,
                score=rrf_scores[chunk_id],
                embedding_model=hit.embedding_model,
                source_type=hit.source_type,
                source_page_start=hit.source_page_start,
                source_page_end=hit.source_page_end,
                source_slide_start=hit.source_slide_start,
                source_slide_end=hit.source_slide_end,
                source_start_seconds=hit.source_start_seconds,
                source_end_seconds=hit.source_end_seconds,
            )
            for chunk_id, hit in source_by_id.items()
        ]

        # 4. RRF 점수 내림차순 정렬 후 top_k 반환
        merged.sort(key=lambda h: h.score, reverse=True)
        return merged[:top_k]

    # FTS(전문 검색) 기반으로 search_chunks를 조회하는 내부 메서드.
    # 형태소 분석된 text_morphemes 컬럼을 우선 사용하여 한국어 검색 정확도를 높인다.
    async def _search_by_keyword(
        self,
        morpheme_query: str,
        transcript_ids: list[UUID],
        user_id: UUID | None,
        top_k: int,
    ) -> list[SearchChunkHit]:

        # 동적 WHERE 절과 파라미터 목록 구성
        # $1은 항상 morpheme_query (ts_rank와 @@ 연산에 공통 사용)
        params: list[Any] = [morpheme_query]
        # text_morphemes가 있는 기존 row라도 원문 text를 함께 검색해 영어/숫자 토큰 누락을 보완한다.
        search_vector_sql = (
            "to_tsvector('simple', "
            "trim(coalesce(sc.text_morphemes, '') || ' ' || coalesce(sc.text, ''))"
            ")"
        )
        where_clauses = [
            f"{search_vector_sql} @@ plainto_tsquery('simple', $1)"
        ]

        # transcript_ids 필터 추가 — search_chunks 테이블에 직접 컬럼 존재
        params.append(transcript_ids)
        where_clauses.append(f"sc.transcript_id = ANY(${len(params)}::uuid[])")

        # user_id 필터 추가 — transcripts 테이블과 JOIN 필요
        needs_join = user_id is not None
        if user_id is not None:
            params.append(user_id)
            where_clauses.append(f"t.user_id = ${len(params)}")

        where_sql = " AND ".join(where_clauses)

        # user_id 필터가 있을 때만 transcripts 테이블 JOIN
        join_sql = (
            "JOIN transcripts t ON sc.transcript_id = t.id"
            if needs_join
            else ""
        )

        query = f"""
            SELECT
                sc.id,
                sc.transcript_id,
                sc.parent_chunk_id,
                sc.child_index,
                sc.start_seconds,
                sc.end_seconds,
                sc.text,
                sc.embedding_model,
                sc.source_type,
                sc.source_page_start,
                sc.source_page_end,
                sc.source_slide_start,
                sc.source_slide_end,
                sc.source_start_seconds,
                sc.source_end_seconds,
                ts_rank(
                    {search_vector_sql},
                    plainto_tsquery('simple', $1)
                ) AS score
            FROM search_chunks sc
            {join_sql}
            WHERE {where_sql}
            ORDER BY score DESC
            LIMIT {top_k}
        """

        rows = await self._connection.fetch(query, *params)

        return [
            SearchChunkHit(
                id=row["id"],
                transcript_id=row["transcript_id"],
                parent_chunk_id=row["parent_chunk_id"],
                child_index=row["child_index"],
                start_seconds=float(row["start_seconds"]) if row["start_seconds"] is not None else None,
                end_seconds=float(row["end_seconds"]) if row["end_seconds"] is not None else None,
                text=row["text"],
                score=float(row["score"]),
                embedding_model=row["embedding_model"],
                source_type=self._row_value(row, "source_type"),
                source_page_start=self._row_value(row, "source_page_start"),
                source_page_end=self._row_value(row, "source_page_end"),
                source_slide_start=self._row_value(row, "source_slide_start"),
                source_slide_end=self._row_value(row, "source_slide_end"),
                source_start_seconds=(
                    float(self._row_value(row, "source_start_seconds"))
                    if self._row_value(row, "source_start_seconds") is not None
                    else None
                ),
                source_end_seconds=(
                    float(self._row_value(row, "source_end_seconds"))
                    if self._row_value(row, "source_end_seconds") is not None
                    else None
                ),
            )
            for row in rows
        ]

    # pgvector 코사인 유사도 기반으로 search_chunks를 조회하는 내부 메서드.
    # 의미적 유사성을 포착하여 키워드 검색이 놓치는 paraphrase/동의어 히트를 보완한다.
    async def _search_by_vector(
        self,
        embedding: list[float],
        transcript_ids: list[UUID],
        user_id: UUID | None,
        top_k: int,
    ) -> list[SearchChunkHit]:
        """
        기능 요약: pgvector <=> 연산자로 코사인 거리를 계산하여 유사 청크를 반환한다.

        기능 흐름:
            1. embedding을 vector literal로 변환
            2. transcript_ids / user_id 필터 조건 동적 추가
            3. distance 오름차순(= score 내림차순), LIMIT top_k 쿼리 실행
            4. score = 1.0 - distance 로 변환하여 반환

        파라미터:
            embedding: 쿼리 임베딩 벡터 (예: [0.1, 0.2, ...], 길이 1536)
            transcript_ids: 검색 범위 한정용 transcript UUID 목록
            user_id: 검색 범위 한정용 user UUID
            top_k: 반환할 최대 결과 수
        """
        # $1은 항상 embedding vector literal
        vector_literal = self._to_vector_literal(embedding)
        params: list[Any] = [vector_literal]
        where_clauses: list[str] = []

        # transcript_ids 필터 추가
        params.append(transcript_ids)
        where_clauses.append(f"sc.transcript_id = ANY(${len(params)}::uuid[])")

        # user_id 필터 추가 — transcripts 테이블과 JOIN 필요
        needs_join = user_id is not None
        if user_id is not None:
            params.append(user_id)
            where_clauses.append(f"t.user_id = ${len(params)}")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # user_id 필터가 있을 때만 transcripts 테이블 JOIN
        join_sql = (
            "JOIN transcripts t ON sc.transcript_id = t.id"
            if needs_join
            else ""
        )

        query = f"""
            SELECT
                sc.id,
                sc.transcript_id,
                sc.parent_chunk_id,
                sc.child_index,
                sc.start_seconds,
                sc.end_seconds,
                sc.text,
                sc.embedding_model,
                sc.source_type,
                sc.source_page_start,
                sc.source_page_end,
                sc.source_slide_start,
                sc.source_slide_end,
                sc.source_start_seconds,
                sc.source_end_seconds,
                (sc.embedding <=> $1::vector) AS distance
            FROM search_chunks sc
            {join_sql}
            {where_sql}
            ORDER BY distance ASC
            LIMIT {top_k}
        """

        rows = await self._connection.fetch(query, *params)

        return [
            SearchChunkHit(
                id=row["id"],
                transcript_id=row["transcript_id"],
                parent_chunk_id=row["parent_chunk_id"],
                child_index=row["child_index"],
                start_seconds=float(row["start_seconds"]) if row["start_seconds"] is not None else None,
                end_seconds=float(row["end_seconds"]) if row["end_seconds"] is not None else None,
                text=row["text"],
                # 코사인 거리(0~2)를 유사도 점수(1~-1)로 변환: 거리가 작을수록 score가 높다
                score=1.0 - float(row["distance"]),
                embedding_model=row["embedding_model"],
                source_type=self._row_value(row, "source_type"),
                source_page_start=self._row_value(row, "source_page_start"),
                source_page_end=self._row_value(row, "source_page_end"),
                source_slide_start=self._row_value(row, "source_slide_start"),
                source_slide_end=self._row_value(row, "source_slide_end"),
                source_start_seconds=(
                    float(self._row_value(row, "source_start_seconds"))
                    if self._row_value(row, "source_start_seconds") is not None
                    else None
                ),
                source_end_seconds=(
                    float(self._row_value(row, "source_end_seconds"))
                    if self._row_value(row, "source_end_seconds") is not None
                    else None
                ),
            )
            for row in rows
        ]

    # 검색 히트된 child chunk의 부모 chunk 전체 문맥을 반환한다.
    # RAG 응답 생성 시 LLM에게 풍부한 메타데이터(topic, keywords 등)를 제공하기 위해 사용한다.
    async def get_parent_chunks(
        self,
        parent_chunk_ids: list[UUID],
    ) -> list[ParentChunkResult]:
        """
        기능 요약: chunks 테이블에서 주어진 UUID 목록에 해당하는 parent chunk를 일괄 조회한다.

        기능 흐름:
            1. 빈 목록이면 즉시 [] 반환
            2. ANY($1::uuid[]) 로 chunks 테이블 일괄 조회
            3. ParentChunkResult 리스트로 매핑하여 반환

        파라미터:
            parent_chunk_ids: 조회할 chunk UUID 목록 (예: [UUID("a1b2..."), UUID("c3d4...")])
        """
        # 1. 빈 목록 조기 반환 — DB 쿼리 불필요
        if not parent_chunk_ids:
            return []

        # 2. ANY 연산자로 일괄 조회 — N+1 쿼리 방지
        # ANY 연산자는 조건에 맞는 값을 모두 리스트 형태로 리턴 가능 Postgresql 배열 타입 캐스팅
        rows = await self._connection.fetch(
            """
            SELECT
                c.id, c.transcript_id, t.title AS transcript_title, c.chunk_index,
                c.topic, c.subtopic, c.keywords, c.speaker_labels,
                c.segment_start_index, c.segment_end_index,
                c.start_seconds, c.end_seconds, c.text, c.summary, c.metadata,
                c.source_type, c.source_page_start, c.source_page_end,
                c.source_slide_start, c.source_slide_end,
                c.source_start_seconds, c.source_end_seconds
            FROM chunks c
            JOIN transcripts t ON c.transcript_id = t.id
            WHERE c.id = ANY($1::uuid[])
            """,
            parent_chunk_ids,
        )

        # 3. ParentChunkResult 리스트로 매핑
        return [
            ParentChunkResult(
                id=row["id"],
                transcript_id=row["transcript_id"],
                transcript_title=row["transcript_title"],
                chunk_index=row["chunk_index"],
                topic=row["topic"],
                subtopic=row["subtopic"],
                keywords=list(row["keywords"]) if row["keywords"] else [],
                speaker_labels=list(row["speaker_labels"]) if row["speaker_labels"] else [],
                segment_start_index=row["segment_start_index"],
                segment_end_index=row["segment_end_index"],
                start_seconds=float(row["start_seconds"]) if row["start_seconds"] is not None else None,
                end_seconds=float(row["end_seconds"]) if row["end_seconds"] is not None else None,
                text=row["text"],
                summary=row["summary"],
                metadata=self._to_dict(row["metadata"]),
                source_type=self._row_value(row, "source_type"),
                source_page_start=self._row_value(row, "source_page_start"),
                source_page_end=self._row_value(row, "source_page_end"),
                source_slide_start=self._row_value(row, "source_slide_start"),
                source_slide_end=self._row_value(row, "source_slide_end"),
                source_start_seconds=(
                    float(self._row_value(row, "source_start_seconds"))
                    if self._row_value(row, "source_start_seconds") is not None
                    else None
                ),
                source_end_seconds=(
                    float(self._row_value(row, "source_end_seconds"))
                    if self._row_value(row, "source_end_seconds") is not None
                    else None
                ),
            )
            for row in rows
        ]

    # 생성된 요약 문서(구조화 payload)를 저장하고 이후 수정/재렌더의 기준 id를 반환한다.
    # payload를 영속화해 두면 동일 transcript 재요청 시 LLM 재호출 없이 PDF만 다시 그릴 수 있다.
    async def insert_summary_document(self, document: SummaryDocumentCreate) -> UUID:
        """
        기능 요약: summary_documents에 1건 insert하고 생성된 id를 반환한다.

        기능 흐름:
            1. uuid4()로 문서 id를 발급
            2. payload는 _to_json()으로 한글 손실 없이 JSONB 직렬화
            3. RETURNING id로 발급된 id를 회수해 반환

        파라미터:
            document: 저장할 문서 (transcript_id, template_id, payload, model 등)
        """
        # 1. 문서 id 발급
        document_id = uuid4()
        row = await self._connection.fetchrow(
            """
            INSERT INTO summary_documents (
              id, transcript_id, user_id, template_id, payload, model
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            RETURNING id
            """,
            document_id,
            document.transcript_id,
            document.user_id,
            document.template_id,
            self._to_json(document.payload),
            document.model,
        )
        return row["id"] if row else document_id

    # transcript 단위 강의 요약 데이터를 조회한다.
    # 이미 생성된 payload가 있으면 API가 LLM 재호출 없이 즉시 반환하기 위해 사용한다.
    async def get_lecture_summary_by_transcript(
        self,
        transcript_id: UUID,
        user_id: UUID | None = None,
    ) -> LectureSummaryDetail | None:
        """
        기능 요약: lecture_summaries에서 transcript_id로 1건 조회한다. user_id가 있으면 소유자까지 필터한다.

        기능 흐름:
            1. user_id 유무에 따라 WHERE 조건을 구성
            2. fetchrow로 기존 강의 요약을 조회
            3. payload(JSONB)를 dict로 정규화해 LectureSummaryDetail로 반환

        파라미터:
            transcript_id: 요약 데이터가 연결된 transcript UUID
            user_id: 소유권 검증용 사용자 UUID. None이면 내부 조회로 간주
        """
        # 1. user_id가 있으면 요약 row의 소유자를 함께 확인한다
        if user_id is not None:
            row = await self._connection.fetchrow(
                """
                SELECT id, transcript_id, user_id, payload, model
                FROM lecture_summaries
                WHERE transcript_id = $1 AND user_id = $2
                ORDER BY created_at DESC
                LIMIT 1
                """,
                transcript_id,
                user_id,
            )
        else:
            row = await self._connection.fetchrow(
                """
                SELECT id, transcript_id, user_id, payload, model
                FROM lecture_summaries
                WHERE transcript_id = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                transcript_id,
            )

        # 2. 기존 요약이 없으면 생성 경로로 진행하도록 None 반환
        if row is None:
            return None

        # 3. JSONB payload를 dict로 정규화
        payload = self._to_dict(row["payload"])

        return LectureSummaryDetail(
            id=row["id"],
            transcript_id=row["transcript_id"],
            user_id=row["user_id"],
            payload=payload,
            model=row["model"],
        )

    # 새 강의 요약 데이터를 lecture_summaries에 저장하고 생성 id를 반환한다.
    # 같은 transcript에 대한 중복 요청은 호출 전에 get_lecture_summary_by_transcript()로 차단한다.
    async def insert_lecture_summary(self, summary: LectureSummaryCreate) -> UUID:
        """
        기능 요약: lecture_summaries에 overview/contexts/keywords payload를 저장한다.

        기능 흐름:
            1. uuid4()로 summary id 발급
            2. payload를 한글 손실 없는 JSONB 문자열로 직렬화
            3. transcript_id unique 충돌 시 기존 payload를 유지하고 기존 id를 반환

        파라미터:
            summary: transcript_id, user_id, payload, model을 담은 저장 모델
        """
        # 1. 요약 id 발급 후 JSONB payload와 함께 저장한다
        summary_id = uuid4()
        row = await self._connection.fetchrow(
            """
            INSERT INTO lecture_summaries (
              id, transcript_id, user_id, payload, model
            )
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (transcript_id) DO UPDATE
            SET updated_at = lecture_summaries.updated_at
            RETURNING id
            """,
            summary_id,
            summary.transcript_id,
            summary.user_id,
            self._to_json(summary.payload),
            summary.model,
        )
        return row["id"] if row else summary_id

    # 저장된 요약 문서를 id로 조회해 수정→재렌더 경로의 입력(template_id + payload)을 제공한다.
    async def get_summary_document_by_id(
        self,
        document_id: UUID,
        user_id: UUID | None = None,
    ) -> SummaryDocumentDetail | None:
        """
        기능 요약: summary_documents에서 id로 1건 조회한다. user_id가 주어지면 소유권까지 필터한다.

        기능 흐름:
            1. user_id 유무에 따라 소유권 필터를 동적으로 추가
            2. fetchrow로 단건 조회 — 없으면 None
            3. payload(JSONB)는 dict로 정규화하여 SummaryDocumentDetail로 매핑

        파라미터:
            document_id: 조회할 문서 UUID
            user_id: 소유권 검증용 사용자 UUID. None이면 소유권 필터 없이 조회
        """
        # 1. 소유권 필터 동적 구성
        if user_id is not None:
            row = await self._connection.fetchrow(
                """
                SELECT id, transcript_id, user_id, template_id, payload, model
                FROM summary_documents
                WHERE id = $1 AND user_id = $2
                """,
                document_id,
                user_id,
            )
        else:
            row = await self._connection.fetchrow(
                """
                SELECT id, transcript_id, user_id, template_id, payload, model
                FROM summary_documents
                WHERE id = $1
                """,
                document_id,
            )

        # 2. 없으면 None — 라우트에서 404로 변환
        if row is None:
            return None

        # 3. payload는 asyncpg가 str(JSON)로 줄 수도, dict로 줄 수도 있으므로 dict로 정규화
        payload = self._to_dict(row["payload"])

        return SummaryDocumentDetail(
            id=row["id"],
            transcript_id=row["transcript_id"],
            user_id=row["user_id"],
            template_id=row["template_id"],
            payload=payload,
            model=row["model"],
        )

    # 저장된 요약 문서의 payload를 수정 내용으로 갱신한다(LLM 재호출 없는 재렌더의 첫 단계).
    async def update_summary_document_payload(
        self,
        document_id: UUID,
        payload: dict[str, Any],
        user_id: UUID | None = None,
    ) -> bool:
        """
        기능 요약: summary_documents.payload를 갱신하고, 갱신된 행 존재 여부를 반환한다.

        기능 흐름:
            1. user_id 유무에 따라 소유권 조건을 동적으로 추가
            2. payload는 _to_json()으로 JSONB 직렬화, updated_at은 now()로 갱신
            3. RETURNING id로 실제 갱신 여부를 판별해 bool 반환 (없으면 False → 라우트 404)

        파라미터:
            document_id: 갱신할 문서 UUID
            payload: 수정된 구조화 요약 (예: {"overview": "...", "decisions": [...]})
            user_id: 소유권 검증용 사용자 UUID
        """
        # 1. 소유권 조건 동적 구성
        if user_id is not None:
            row = await self._connection.fetchrow(
                """
                UPDATE summary_documents
                SET payload = $2::jsonb, updated_at = now()
                WHERE id = $1 AND user_id = $3
                RETURNING id
                """,
                document_id,
                self._to_json(payload),
                user_id,
            )
        else:
            row = await self._connection.fetchrow(
                """
                UPDATE summary_documents
                SET payload = $2::jsonb, updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                document_id,
                self._to_json(payload),
            )

        # 2. 갱신된 행이 있으면 True
        return row is not None

    # JSONB column에 넣을 metadata를 한글 손실 없이 문자열로 직렬화한다.
    def _to_json(self, value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False)

    # JSONB 조회 결과는 실행 환경에 따라 dict 또는 JSON 문자열로 들어올 수 있다.
    def _to_dict(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        try:
            return dict(value)
        except (TypeError, ValueError):
            return {}

    def _row_value(self, row: Any, key: str) -> Any:
        try:
            return row[key]
        except (KeyError, TypeError):
            return None

    # pgvector가 adapter 없이도 받을 수 있는 literal 문자열로 embedding을 직렬화한다.
    def _to_vector_literal(self, embedding: list[float] | None) -> str | None:
        if embedding is None:
            return None
        return "[" + ",".join(str(value) for value in embedding) + "]"
