import json
from typing import Any
from uuid import UUID, uuid4

from db.connection import DatabaseConnection
from schemas.rag import ChunkCreate, ChunkRow, SearchChunkCreate, SegmentCreate, TranscriptCreate, TranscriptResultUpdate


class RagRepository:
    def __init__(self, connection: DatabaseConnection) -> None:
        self._connection = connection

    # transcript source-of-truth row를 만들고 이후 STT/segment/chunk 저장의 기준 id를 반환한다.
    async def create_transcript(self, transcript: TranscriptCreate) -> UUID:
        transcript_id = uuid4()
        row = await self._connection.fetchrow(
            """
            INSERT INTO transcripts (
              id, user_id, domain_type, title, source_audio_uri,
              original_filename, mime_type, duration_seconds, language,
              stt_model, status
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            transcript_id,
            transcript.user_id,
            transcript.domain_type,
            transcript.title,
            transcript.source_audio_uri,
            transcript.original_filename,
            transcript.mime_type,
            transcript.duration_seconds,
            transcript.language,
            transcript.stt_model,
            transcript.status,
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
              start_seconds, end_seconds, text, confidence, raw_metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            ON CONFLICT (transcript_id, segment_index) DO UPDATE
            SET speaker_label = EXCLUDED.speaker_label,
                start_seconds = EXCLUDED.start_seconds,
                end_seconds = EXCLUDED.end_seconds,
                text = EXCLUDED.text,
                confidence = EXCLUDED.confidence,
                raw_metadata = EXCLUDED.raw_metadata
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
              id, transcript_id, domain_type, chunk_index, chunk_strategy,
              segment_start_index, segment_end_index, start_seconds, end_seconds,
              text, summary, topic, subtopic, keywords, speaker_labels,
              metadata, embedding_model, embedding
            )
            VALUES (
              $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
              $11, $12, $13, $14, $15, $16::jsonb, $17, $18::vector
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
                embedding = EXCLUDED.embedding
            """,
            [
                (
                    uuid4(),
                    transcript_id,
                    chunk.domain_type,
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
                )
                for chunk in chunks
            ],
        )

    # insert_chunks() 완료 후 search_chunks 생성에 필요한 parent_chunk_id를 얻기 위해 조회한다.
    async def fetch_chunks_by_transcript(self, transcript_id: UUID) -> list[ChunkRow]:
        rows = await self._connection.fetch(
            """
            SELECT id, chunk_index, segment_start_index, segment_end_index,
                   start_seconds, end_seconds, text, metadata
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
                segment_start_index=row["segment_start_index"],
                segment_end_index=row["segment_end_index"],
                start_seconds=float(row["start_seconds"]) if row["start_seconds"] is not None else None,
                end_seconds=float(row["end_seconds"]) if row["end_seconds"] is not None else None,
                text=row["text"],
                metadata=row["metadata"] if row["metadata"] is not None else {},
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
        await self._connection.executemany(
            """
            INSERT INTO search_chunks (
              id, transcript_id, parent_chunk_id, child_index,
              segment_start_index, segment_end_index, start_seconds, end_seconds,
              text, embedding_model, embedding, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::vector, $12::jsonb)
            ON CONFLICT (parent_chunk_id, child_index) DO UPDATE
            SET segment_start_index = EXCLUDED.segment_start_index,
                segment_end_index = EXCLUDED.segment_end_index,
                start_seconds = EXCLUDED.start_seconds,
                end_seconds = EXCLUDED.end_seconds,
                text = EXCLUDED.text,
                embedding_model = EXCLUDED.embedding_model,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata
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
                    chunk.embedding_model,
                    self._to_vector_literal(chunk.embedding),
                    self._to_json(chunk.metadata),
                )
                for chunk in search_chunks
            ],
        )

    # JSONB column에 넣을 metadata를 한글 손실 없이 문자열로 직렬화한다.
    def _to_json(self, value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False)

    # pgvector가 adapter 없이도 받을 수 있는 literal 문자열로 embedding을 직렬화한다.
    def _to_vector_literal(self, embedding: list[float] | None) -> str | None:
        if embedding is None:
            return None
        return "[" + ",".join(str(value) for value in embedding) + "]"
