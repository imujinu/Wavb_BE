import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

from db.connection import DatabaseConnection
from schemas.rag import ChunkCreate, ChunkRow, ParentChunkResult, SearchChunkCreate, SearchChunkHit, SegmentCreate, TranscriptCreate, TranscriptResultUpdate


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
        # text_morphemes($10): 형태소 분석 결과 텍스트, NULL 허용 (FTS에서 coalesce로 text fallback)
        await self._connection.executemany(
            """
            INSERT INTO search_chunks (
              id, transcript_id, parent_chunk_id, child_index,
              segment_start_index, segment_end_index, start_seconds, end_seconds,
              text, text_morphemes, embedding_model, embedding, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::vector, $13::jsonb)
            ON CONFLICT (parent_chunk_id, child_index) DO UPDATE
            SET segment_start_index = EXCLUDED.segment_start_index,
                segment_end_index = EXCLUDED.segment_end_index,
                start_seconds = EXCLUDED.start_seconds,
                end_seconds = EXCLUDED.end_seconds,
                text = EXCLUDED.text,
                text_morphemes = EXCLUDED.text_morphemes,
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
                    chunk.text_morphemes,
                    chunk.embedding_model,
                    self._to_vector_literal(chunk.embedding),
                    self._to_json(chunk.metadata),
                )
                for chunk in search_chunks
            ],
        )

    # keyword 검색과 vector 검색을 병렬로 실행하여 가중 합산 점수로 최종 순위를 결정한다.
    # 단일 검색 방식보다 recall/precision 균형이 높아 RAG 품질을 향상시킨다.
    async def search_chunks_hybrid(
        self,
        morpheme_query: str,
        embedding: list[float],
        transcript_id: UUID | None,
        user_id: UUID | None,
        top_k: int,
        keyword_weight: float = 0.6,
        vector_weight: float = 0.4,
    ) -> list[SearchChunkHit]:
      
        # 1. keyword 검색과 vector 검색을 병렬 실행
        keyword_hits, vector_hits = await asyncio.gather(
            self._search_by_keyword(morpheme_query, transcript_id, user_id, top_k),
            self._search_by_vector(embedding, transcript_id, user_id, top_k),
        )

        # 2. 두 결과 모두 id → (hit, raw_score) 딕셔너리로 변환 — 구조 통일로 O(1) 조회 보장
        keyword_scores: dict[UUID, tuple[SearchChunkHit, float]] = {
            hit.id: (hit, hit.score) for hit in keyword_hits
        }
        vector_scores: dict[UUID, tuple[SearchChunkHit, float]] = {
            hit.id: (hit, hit.score) for hit in vector_hits
        }

        # 3. 모든 hit id를 합집합으로 수집하여 가중 합산 점수 계산
        all_ids = set(keyword_scores) | set(vector_scores)
        merged: list[SearchChunkHit] = []
        for chunk_id in all_ids:
            kw_hit, kw_raw = keyword_scores.get(chunk_id, (None, 0.0))
            vec_hit, vec_raw = vector_scores.get(chunk_id, (None, 0.0))
            source_hit = kw_hit or vec_hit

            merged.append(
                SearchChunkHit(
                    id=source_hit.id,
                    transcript_id=source_hit.transcript_id,
                    parent_chunk_id=source_hit.parent_chunk_id,
                    child_index=source_hit.child_index,
                    start_seconds=source_hit.start_seconds,
                    end_seconds=source_hit.end_seconds,
                    text=source_hit.text,
                    score=keyword_weight * kw_raw + vector_weight * vec_raw,
                    embedding_model=source_hit.embedding_model,
                )
            )

        # 5. score 내림차순 정렬 후 top_k 반환
        merged.sort(key=lambda h: h.score, reverse=True)
        return merged[:top_k]

    # FTS(전문 검색) 기반으로 search_chunks를 조회하는 내부 메서드.
    # 형태소 분석된 text_morphemes 컬럼을 우선 사용하여 한국어 검색 정확도를 높인다.
    async def _search_by_keyword(
        self,
        morpheme_query: str,
        transcript_id: UUID | None,
        user_id: UUID | None,
        top_k: int,
    ) -> list[SearchChunkHit]:

        # 동적 WHERE 절과 파라미터 목록 구성
        # $1은 항상 morpheme_query (ts_rank와 @@ 연산에 공통 사용)
        params: list[Any] = [morpheme_query]
        where_clauses = [
            "to_tsvector('simple', coalesce(sc.text_morphemes, sc.text)) "
            "@@ plainto_tsquery('simple', $1)"
        ]

        # transcript_id 필터 추가 — search_chunks 테이블에 직접 컬럼 존재
        if transcript_id is not None:
            params.append(transcript_id)
            where_clauses.append(f"sc.transcript_id = ${len(params)}")

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
                ts_rank(
                    to_tsvector('simple', coalesce(sc.text_morphemes, sc.text)),
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
            )
            for row in rows
        ]

    # pgvector 코사인 유사도 기반으로 search_chunks를 조회하는 내부 메서드.
    # 의미적 유사성을 포착하여 키워드 검색이 놓치는 paraphrase/동의어 히트를 보완한다.
    async def _search_by_vector(
        self,
        embedding: list[float],
        transcript_id: UUID | None,
        user_id: UUID | None,
        top_k: int,
    ) -> list[SearchChunkHit]:
        """
        기능 요약: pgvector <=> 연산자로 코사인 거리를 계산하여 유사 청크를 반환한다.

        기능 흐름:
            1. embedding을 vector literal로 변환
            2. transcript_id / user_id 필터 조건 동적 추가
            3. distance 오름차순(= score 내림차순), LIMIT top_k 쿼리 실행
            4. score = 1.0 - distance 로 변환하여 반환

        파라미터:
            embedding: 쿼리 임베딩 벡터 (예: [0.1, 0.2, ...], 길이 1536)
            transcript_id: 검색 범위 한정용 transcript UUID
            user_id: 검색 범위 한정용 user UUID
            top_k: 반환할 최대 결과 수
        """
        # $1은 항상 embedding vector literal
        vector_literal = self._to_vector_literal(embedding)
        params: list[Any] = [vector_literal]
        where_clauses: list[str] = []

        # transcript_id 필터 추가
        if transcript_id is not None:
            params.append(transcript_id)
            where_clauses.append(f"sc.transcript_id = ${len(params)}")

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
        rows = await self._connection.fetch(
            """
            SELECT
                id, transcript_id, domain_type, chunk_index,
                topic, subtopic, keywords, speaker_labels,
                start_seconds, end_seconds, text, summary, metadata
            FROM chunks
            WHERE id = ANY($1::uuid[])
            """,
            parent_chunk_ids,
        )

        # 3. ParentChunkResult 리스트로 매핑
        return [
            ParentChunkResult(
                id=row["id"],
                transcript_id=row["transcript_id"],
                domain_type=row["domain_type"],
                chunk_index=row["chunk_index"],
                topic=row["topic"],
                subtopic=row["subtopic"],
                keywords=list(row["keywords"]) if row["keywords"] else [],
                speaker_labels=list(row["speaker_labels"]) if row["speaker_labels"] else [],
                start_seconds=float(row["start_seconds"]) if row["start_seconds"] is not None else None,
                end_seconds=float(row["end_seconds"]) if row["end_seconds"] is not None else None,
                text=row["text"],
                summary=row["summary"],
                metadata=dict(row["metadata"]) if row["metadata"] is not None else {},
            )
            for row in rows
        ]

    # JSONB column에 넣을 metadata를 한글 손실 없이 문자열로 직렬화한다.
    def _to_json(self, value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False)

    # pgvector가 adapter 없이도 받을 수 있는 literal 문자열로 embedding을 직렬화한다.
    def _to_vector_literal(self, embedding: list[float] | None) -> str | None:
        if embedding is None:
            return None
        return "[" + ",".join(str(value) for value in embedding) + "]"
