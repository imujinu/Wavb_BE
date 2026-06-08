from pathlib import Path
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from db import connection as db_connection
from repositories.rag_repository import RagRepository
from schemas.rag import (
    ChunkCreate,
    LectureSummaryCreate,
    SearchChunkCreate,
    SegmentCreate,
    SummaryDocumentCreate,
    TranscriptCreate,
    TranscriptResultUpdate,
)
from settings import Settings


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        # fetch 호출 시 반환할 가짜 rows 목록 (테스트에서 직접 설정)
        self.fetch_results: list[list[dict]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.executed.append((query, args))
        return {"id": args[0]}

    async def execute(self, query: str, *args):
        self.executed.append((query, args))

    async def executemany(self, query: str, args: list[tuple]):
        self.executemany_calls.append((query, args))

    async def fetch(self, query: str, *args):
        # fetch_results에 미리 세팅된 값이 있으면 순서대로 반환, 없으면 빈 리스트
        self.fetch_calls.append((query, args))
        if self.fetch_results:
            return self.fetch_results.pop(0)
        return []


class FakeAcquireContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeConnection:
        self.entered = True
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exited = True


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.acquire_context = FakeAcquireContext(connection)

    def acquire(self) -> FakeAcquireContext:
        return self.acquire_context


class FakeDatabase:
    def __init__(self, connection: FakeConnection) -> None:
        self.pool = FakePool(connection)
        self.connected = False

    async def connect(self) -> None:
        self.connected = True


def test_settings_reads_database_url() -> None:
    settings = Settings(DATABASE_URL="postgresql://recordoc:test@localhost:5432/recordoc")

    assert settings.database_url == "postgresql://recordoc:test@localhost:5432/recordoc"


def test_rag_migration_defines_step1_tables_and_indexes() -> None:
    migration = Path("db/migrations/001_create_rag_persistence.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS transcripts" in migration
    assert "CREATE TABLE IF NOT EXISTS segments" in migration
    assert "CREATE TABLE IF NOT EXISTS chunks" in migration
    assert "CREATE TABLE IF NOT EXISTS lecture_summaries" in migration
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "embedding vector(1536)" in migration
    assert "idx_chunks_text_fts" in migration
    assert "idx_chunks_embedding" in migration
    assert "domain_type" not in migration


def test_source_range_migration_adds_nullable_columns() -> None:
    migration = Path("db/migrations/009_add_source_range_columns.sql").read_text()

    assert "ALTER TABLE segments" in migration
    assert "ALTER TABLE chunks" in migration
    assert "ALTER TABLE search_chunks" in migration
    assert "source_type TEXT" in migration
    assert "source_page_start INT" in migration
    assert "source_slide_start INT" in migration
    assert "source_start_seconds NUMERIC" in migration


def test_lazy_processing_migration_adds_status_and_temporary_segments() -> None:
    migration = Path("db/migrations/011_add_lazy_processing_status.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS source_type TEXT" in migration
    assert "ADD COLUMN IF NOT EXISTS content_status TEXT" in migration
    assert "ADD COLUMN IF NOT EXISTS index_status TEXT" in migration
    assert "CREATE TABLE IF NOT EXISTS temporary_segments" in migration
    assert "UNIQUE (transcript_id, segment_index)" in migration


def test_processing_cancellation_migration_adds_cancel_columns() -> None:
    migration = Path("db/migrations/012_add_processing_cancellation.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS cancel_requested_at" in migration
    assert "ADD COLUMN IF NOT EXISTS cancelled_at" in migration
    assert "idx_transcripts_cancel_requested" in migration


def test_pydantic_models_validate_domain_and_ranges() -> None:
    assert TranscriptCreate(source_audio_uri="uploads/a.mp3")

    with pytest.raises(ValidationError):
        SegmentCreate(
            segment_index=0,
            start_seconds=10,
            end_seconds=9,
            text="invalid range",
        )

    with pytest.raises(ValidationError):
        ChunkCreate(
            chunk_index=0,
            chunk_strategy="lecture_v1",
            text="invalid range",
            segment_start_index=2,
            segment_end_index=1,
        )


def test_chunk_model_normalizes_blank_metadata_lists() -> None:
    chunk = ChunkCreate(
        chunk_index=0,
        chunk_strategy="lecture_topic_v1",
        text="개념 설명",
        keywords=[" 개념 ", ""],
        speaker_labels=[" speaker_1 ", ""],
    )

    assert chunk.keywords == ["개념"]
    assert chunk.speaker_labels == ["speaker_1"]


@pytest.mark.asyncio
async def test_get_connection_connects_and_acquires_pool(monkeypatch) -> None:
    fake_connection = FakeConnection()
    fake_database = FakeDatabase(fake_connection)
    monkeypatch.setattr(db_connection, "_database", fake_database)

    yielded_connections = []
    async for connection in db_connection.get_connection():
        yielded_connections.append(connection)

    assert fake_database.connected is True
    assert yielded_connections == [fake_connection]
    assert fake_database.pool.acquire_context.entered is True
    assert fake_database.pool.acquire_context.exited is True


@pytest.mark.asyncio
async def test_repository_creates_transcript_and_updates_result() -> None:
    connection = FakeConnection()
    repository = RagRepository(connection)

    transcript_id = await repository.create_transcript(
        TranscriptCreate(
            source_audio_uri="uploads/meeting.mp3",
            original_filename="meeting.mp3",
            mime_type="audio/mpeg",
            status="uploaded",
        )
    )
    await repository.update_transcript_result(
        transcript_id,
        TranscriptResultUpdate(
            full_text="회의 내용",
            summary="회의 요약",
            duration_seconds=123.4,
            stt_model="whisper-1",
        ),
    )

    assert isinstance(transcript_id, UUID)
    assert "INSERT INTO transcripts" in connection.executed[0][0]
    assert "folder_id" in connection.executed[0][0]
    assert "content_status" in connection.executed[0][0]
    assert connection.executed[0][1][3] == "uploads/meeting.mp3"
    assert connection.executed[0][1][10] is None
    assert connection.executed[0][1][11] is None
    assert connection.executed[0][1][12] == "pending"
    assert connection.executed[0][1][13] == "pending"
    assert connection.executed[0][1][14] is None
    assert "UPDATE transcripts" in connection.executed[1][0]
    assert connection.executed[1][1][1] == "회의 내용"
    assert connection.executed[1][1][5] == "completed"


@pytest.mark.asyncio
async def test_repository_requests_processing_cancel_and_checks_flag() -> None:
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()
    user_id = uuid4()

    updated = await repository.request_processing_cancel(transcript_id, user_id)
    is_cancelled = await repository.is_processing_cancel_requested(
        transcript_id,
        user_id,
    )
    is_cancelled_after = await repository.is_processing_cancel_requested_after(
        transcript_id,
        user_id,
        datetime.now(timezone.utc),
    )

    assert updated is True
    assert is_cancelled is True
    assert is_cancelled_after is True
    assert "cancel_requested_at" in connection.executed[0][0]
    assert "cancelled_at" in connection.executed[0][0]
    assert "cancel_requested_at IS NOT NULL" in connection.executed[1][0]
    assert "cancel_requested_at >= $3" in connection.executed[2][0]
    assert connection.executed[0][1] == (transcript_id, user_id)
    assert connection.executed[1][1] == (transcript_id, user_id)


@pytest.mark.asyncio
async def test_repository_inserts_segments_and_chunks() -> None:
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()

    await repository.insert_segments(
        transcript_id,
        [
            SegmentCreate(
                segment_index=0,
                speaker_label="speaker_1",
                start_seconds=0.0,
                end_seconds=4.2,
            text="첫 번째 발화",
            confidence=0.91,
            raw_metadata={"provider": "openai"},
            source_type="audio",
            source_start_seconds=0.0,
            source_end_seconds=4.2,
        )
        ],
    )
    await repository.insert_chunks(
        transcript_id,
        [
            ChunkCreate(
                chunk_index=0,
                chunk_strategy="lecture_context_plan_v1",
                segment_start_index=0,
                segment_end_index=0,
                start_seconds=0.0,
                end_seconds=4.2,
                text="첫 번째 발화",
                summary="첫 번째 발화 요약",
                topic="일정",
                keywords=["일정"],
                speaker_labels=["speaker_1"],
                metadata={"decision_flag": True},
                embedding_model="text-embedding-3-small",
                embedding=[0.1, 0.2, 0.3],
                source_type="pdf",
                source_page_start=1,
                source_page_end=2,
            )
        ],
    )

    assert "INSERT INTO segments" in connection.executemany_calls[0][0]
    assert connection.executemany_calls[0][1][0][1] == transcript_id
    assert connection.executemany_calls[0][1][0][8] == '{"provider": "openai"}'
    assert connection.executemany_calls[0][1][0][9] == "audio"
    assert connection.executemany_calls[0][1][0][14] == 0.0
    assert connection.executemany_calls[0][1][0][15] == 4.2
    assert "INSERT INTO chunks" in connection.executemany_calls[1][0]
    assert connection.executemany_calls[1][1][0][2] == 0
    assert connection.executemany_calls[1][1][0][12] == ["일정"]
    assert connection.executemany_calls[1][1][0][16] == "[0.1,0.2,0.3]"
    assert connection.executemany_calls[1][1][0][17] == "pdf"
    assert connection.executemany_calls[1][1][0][18] == 1
    assert connection.executemany_calls[1][1][0][19] == 2


@pytest.mark.asyncio
async def test_insert_search_chunks_creates_rows() -> None:
    # insert_search_chunks가 executemany를 올바른 SQL과 파라미터로 호출하는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()
    parent_chunk_id = uuid4()

    search_chunks = [
        SearchChunkCreate(
            parent_chunk_id=parent_chunk_id,
            child_index=0,
            segment_start_index=0,
            segment_end_index=2,
            start_seconds=0.0,
            end_seconds=30.0,
            text="첫 번째 검색 청크",
            metadata={"segment_count": 3},
            embedding_model="text-embedding-3-small",
            embedding=[0.1, 0.2, 0.3],
            source_type="ppt",
            source_slide_start=1,
            source_slide_end=1,
        ),
        SearchChunkCreate(
            parent_chunk_id=parent_chunk_id,
            child_index=1,
            segment_start_index=3,
            segment_end_index=5,
            start_seconds=30.0,
            end_seconds=60.0,
            text="두 번째 검색 청크",
            metadata={"segment_count": 3},
            embedding_model="text-embedding-3-small",
            embedding=[0.4, 0.5, 0.6],
            source_type="ppt",
            source_slide_start=2,
            source_slide_end=3,
        ),
    ]

    await repository.insert_search_chunks(transcript_id, search_chunks)

    # executemany가 정확히 1회 호출되었는지 확인
    assert len(connection.executemany_calls) == 1

    sql, params = connection.executemany_calls[0]

    # SQL에 필수 키워드가 포함되어 있는지 확인
    assert "INSERT INTO search_chunks" in sql
    assert "ON CONFLICT (parent_chunk_id, child_index)" in sql

    # 첫 번째 행 파라미터 매핑 검증: $2=transcript_id, $3=parent_chunk_id, $4=child_index
    # text_morphemes($10, index 9) 추가로 embedding_model은 index 10, embedding은 index 11, metadata는 index 12로 이동
    first_row = params[0]
    assert first_row[1] == transcript_id
    assert first_row[2] == parent_chunk_id
    assert first_row[3] == 0
    assert first_row[8] == "첫 번째 검색 청크"
    # index 9: text_morphemes. 형태소 분석 값이 없으면 NULL 그대로 저장한다.
    assert first_row[9] is None
    # index 10: embedding_model
    assert first_row[10] == "text-embedding-3-small"
    assert first_row[13] == "ppt"
    assert first_row[16] == 1
    assert first_row[17] == 1

    # 두 번째 행도 정상 저장되는지 확인
    assert params[1][3] == 1
    assert params[1][8] == "두 번째 검색 청크"
    assert params[1][16] == 2
    assert params[1][17] == 3


@pytest.mark.asyncio
async def test_insert_search_chunks_empty_list_does_nothing() -> None:
    # 빈 목록 전달 시 executemany를 호출하지 않고 조기 반환하는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()

    await repository.insert_search_chunks(transcript_id, [])

    assert len(connection.executemany_calls) == 0


@pytest.mark.asyncio
async def test_insert_search_chunks_uses_vector_literal() -> None:
    # embedding 벡터가 _to_vector_literal() 형식("[0.1,0.2,0.3]")으로 변환되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()

    await repository.insert_search_chunks(
        transcript_id,
        [
            SearchChunkCreate(
                parent_chunk_id=uuid4(),
                child_index=0,
                segment_start_index=0,
                segment_end_index=1,
                text="벡터 직렬화 테스트",
                embedding_model="text-embedding-3-small",
                embedding=[0.1, 0.2, 0.3],
            )
        ],
    )

    # text_morphemes 추가로 embedding은 $12 위치(index 11)로 이동
    row = connection.executemany_calls[0][1][0]
    assert row[11] == "[0.1,0.2,0.3]"


@pytest.mark.asyncio
async def test_insert_search_chunks_uses_json_metadata() -> None:
    # metadata dict가 _to_json() 형식(한글 손실 없는 JSON string)으로 변환되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()

    await repository.insert_search_chunks(
        transcript_id,
        [
            SearchChunkCreate(
                parent_chunk_id=uuid4(),
                child_index=0,
                segment_start_index=0,
                segment_end_index=1,
                text="메타데이터 직렬화 테스트",
                embedding_model="text-embedding-3-small",
                metadata={"주제": "회의", "segment_count": 2},
            )
        ],
    )

    # text_morphemes 추가로 metadata는 $13 위치(index 12)로 이동
    row = connection.executemany_calls[0][1][0]
    assert row[12] == '{"주제": "회의", "segment_count": 2}'


# --- search_chunks_hybrid / _search_by_keyword / _search_by_vector / get_parent_chunks 테스트 ---


def _make_search_chunk_row(
    chunk_id: UUID,
    transcript_id: UUID,
    parent_chunk_id: UUID,
    score_field: float,
    field_name: str = "score",
) -> dict:
    """테스트용 search_chunks 조회 결과 row를 생성하는 헬퍼 함수"""
    return {
        "id": chunk_id,
        "transcript_id": transcript_id,
        "parent_chunk_id": parent_chunk_id,
        "child_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 30.0,
        "text": "테스트 청크 텍스트",
        "embedding_model": "text-embedding-3-small",
        field_name: score_field,
    }


@pytest.mark.asyncio
async def test_search_chunks_hybrid_merges_keyword_and_vector_scores() -> None:
    # keyword와 vector 양쪽 모두 hit된 경우 가중 합산 점수가 올바르게 계산되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    transcript_id = uuid4()
    parent_id = uuid4()
    chunk_id = uuid4()

    # keyword 검색 결과: score=0.8, vector 검색 결과: score=0.9 으로 동일 id hit
    kw_row = _make_search_chunk_row(chunk_id, transcript_id, parent_id, 0.8, "score")
    vec_row = _make_search_chunk_row(chunk_id, transcript_id, parent_id, 0.9, "distance")
    # vec_row의 "distance" 필드를 사용하므로 별도 구성
    vec_row_actual = {**kw_row, "distance": 0.1}  # score = 1.0 - 0.1 = 0.9

    # fetch 첫 번째 호출: keyword 결과, 두 번째 호출: vector 결과
    connection.fetch_results = [[kw_row], [vec_row_actual]]

    hits = await repository.search_chunks_hybrid(
        morpheme_query="테스트 쿼리",
        embedding=[0.1] * 1536,
        transcript_ids=[transcript_id],
        user_id=None,
        top_k=5,
    )

    assert len(hits) == 1
    # 예상 score (RRF): keyword_weight/(k+1) + vector_weight/(k+1) = 1.0/61
    assert abs(hits[0].score - (1.0 / 61)) < 1e-6
    assert hits[0].id == chunk_id


@pytest.mark.asyncio
async def test_search_chunks_hybrid_keyword_only_hit() -> None:
    # keyword에만 hit된 경우 keyword_weight * kw_score만 적용되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    transcript_id = uuid4()
    parent_id = uuid4()
    chunk_id = uuid4()

    kw_row = _make_search_chunk_row(chunk_id, transcript_id, parent_id, 0.75, "score")
    # vector 검색 결과는 빈 리스트 (hit 없음)
    connection.fetch_results = [[kw_row], []]

    hits = await repository.search_chunks_hybrid(
        morpheme_query="키워드 전용",
        embedding=[0.2] * 1536,
        transcript_ids=[transcript_id],
        user_id=None,
        top_k=5,
    )

    assert len(hits) == 1
    # 예상 score (RRF keyword only): keyword_weight/(k+1) = 0.6/61
    assert abs(hits[0].score - (0.6 / 61)) < 1e-6


@pytest.mark.asyncio
async def test_search_chunks_hybrid_vector_only_hit() -> None:
    # vector에만 hit된 경우 vector_weight * vec_score만 적용되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    transcript_id = uuid4()
    parent_id = uuid4()
    chunk_id = uuid4()

    # distance=0.2 → score = 1.0 - 0.2 = 0.8
    vec_row = {
        "id": chunk_id,
        "transcript_id": transcript_id,
        "parent_chunk_id": parent_id,
        "child_index": 0,
        "start_seconds": None,
        "end_seconds": None,
        "text": "벡터 전용 히트",
        "embedding_model": "text-embedding-3-small",
        "distance": 0.2,
    }
    # keyword 검색은 빈 결과, vector 검색에 hit
    connection.fetch_results = [[], [vec_row]]

    hits = await repository.search_chunks_hybrid(
        morpheme_query="벡터 전용",
        embedding=[0.3] * 1536,
        transcript_ids=[transcript_id],
        user_id=None,
        top_k=5,
    )

    assert len(hits) == 1
    # 예상 score (RRF vector only): vector_weight/(k+1) = 0.4/61
    assert abs(hits[0].score - (0.4 / 61)) < 1e-6
    assert hits[0].start_seconds is None
    assert hits[0].end_seconds is None


@pytest.mark.asyncio
async def test_search_chunks_hybrid_returns_top_k() -> None:
    # top_k 제한이 올바르게 적용되는지 검증한다 (결과가 top_k를 초과하지 않음).
    connection = FakeConnection()
    repository = RagRepository(connection)

    transcript_id = uuid4()
    parent_id = uuid4()

    # keyword: 3개 hit, vector: 3개 hit (모두 다른 id)
    kw_rows = [
        _make_search_chunk_row(uuid4(), transcript_id, parent_id, 0.9 - i * 0.1, "score")
        for i in range(3)
    ]
    vec_rows = [
        {**_make_search_chunk_row(uuid4(), transcript_id, parent_id, 0.1, "score"), "distance": 0.1}
        for _ in range(3)
    ]
    connection.fetch_results = [kw_rows, vec_rows]

    hits = await repository.search_chunks_hybrid(
        morpheme_query="top_k 테스트",
        embedding=[0.1] * 1536,
        transcript_ids=[transcript_id],
        user_id=None,
        top_k=4,
    )

    # 총 6개 고유 id이지만 top_k=4이므로 최대 4개 반환
    assert len(hits) <= 4
    # score 내림차순 정렬 검증
    for i in range(len(hits) - 1):
        assert hits[i].score >= hits[i + 1].score


@pytest.mark.asyncio
async def test_search_by_keyword_builds_correct_sql() -> None:
    # _search_by_keyword가 transcript_ids 배열 필터와 FTS SQL을 생성하는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()

    connection.fetch_results = [[]]
    await repository._search_by_keyword(
        morpheme_query="회의 일정",
        transcript_ids=[transcript_id],
        user_id=None,
        top_k=5,
    )

    assert len(connection.fetch_calls) == 1
    sql, args = connection.fetch_calls[0]
    # FTS 쿼리 핵심 요소 포함 여부 검증
    assert "plainto_tsquery" in sql
    assert "ts_rank" in sql
    assert "text_morphemes" in sql
    assert "coalesce(sc.text_morphemes, '')" in sql
    assert "coalesce(sc.text, '')" in sql
    assert "sc.transcript_id = ANY" in sql
    assert args[0] == "회의 일정"
    assert args[1] == [transcript_id]


@pytest.mark.asyncio
async def test_search_by_keyword_adds_transcript_filter() -> None:
    # transcript_ids가 주어졌을 때 WHERE 절에 ANY 배열 필터가 추가되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    transcript_id = uuid4()
    connection.fetch_results = [[]]
    await repository._search_by_keyword(
        morpheme_query="필터 테스트",
        transcript_ids=[transcript_id],
        user_id=None,
        top_k=5,
    )

    sql, args = connection.fetch_calls[0]
    assert "sc.transcript_id = ANY" in sql
    # args[0]=morpheme_query, args[1]=transcript_ids
    assert args[1] == [transcript_id]


@pytest.mark.asyncio
async def test_search_by_keyword_adds_user_id_join() -> None:
    # user_id가 주어졌을 때 transcripts JOIN이 포함되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    user_id = uuid4()
    connection.fetch_results = [[]]
    await repository._search_by_keyword(
        morpheme_query="사용자 필터",
        transcript_ids=[uuid4()],
        user_id=user_id,
        top_k=5,
    )

    sql, args = connection.fetch_calls[0]
    assert "JOIN transcripts t" in sql
    assert "t.user_id" in sql
    assert args[2] == user_id


@pytest.mark.asyncio
async def test_search_by_vector_builds_correct_sql() -> None:
    # _search_by_vector가 pgvector <=> 연산자를 포함한 SQL을 생성하는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()

    connection.fetch_results = [[]]
    await repository._search_by_vector(
        embedding=[0.1] * 5,
        transcript_ids=[transcript_id],
        user_id=None,
        top_k=5,
    )

    sql, args = connection.fetch_calls[0]
    assert "<=>" in sql
    assert "::vector" in sql
    assert "distance" in sql
    assert "sc.transcript_id = ANY" in sql
    # embedding이 vector literal 형식으로 변환되어 첫 번째 인자로 전달되는지 확인
    assert args[0].startswith("[")
    assert args[0].endswith("]")
    assert args[1] == [transcript_id]


@pytest.mark.asyncio
async def test_search_by_vector_score_is_one_minus_distance() -> None:
    # distance=0.3이면 score=0.7이 반환되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    transcript_id = uuid4()
    parent_id = uuid4()
    chunk_id = uuid4()

    connection.fetch_results = [[{
        "id": chunk_id,
        "transcript_id": transcript_id,
        "parent_chunk_id": parent_id,
        "child_index": 1,
        "start_seconds": 10.0,
        "end_seconds": 40.0,
        "text": "거리-점수 변환 테스트",
        "embedding_model": "text-embedding-3-small",
        "distance": 0.3,
    }]]

    hits = await repository._search_by_vector(
        embedding=[0.1] * 5,
        transcript_ids=[transcript_id],
        user_id=None,
        top_k=5,
    )

    assert len(hits) == 1
    assert abs(hits[0].score - 0.7) < 1e-6
    assert hits[0].child_index == 1


@pytest.mark.asyncio
async def test_get_parent_chunks_empty_ids_returns_empty() -> None:
    # 빈 목록 전달 시 DB 쿼리 없이 즉시 [] 반환하는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    result = await repository.get_parent_chunks([])

    assert result == []
    assert len(connection.fetch_calls) == 0


@pytest.mark.asyncio
async def test_get_parent_chunks_queries_chunks_table() -> None:
    # parent_chunk_ids를 ANY($1::uuid[]) 로 조회하는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    chunk_id = uuid4()
    transcript_id = uuid4()

    connection.fetch_results = [[{
        "id": chunk_id,
        "transcript_id": transcript_id,
        "transcript_title": "주간 프로젝트 강의",
        "chunk_index": 0,
        "topic": "일정",
        "subtopic": "주간 회의",
        "keywords": ["일정", "프로젝트"],
        "speaker_labels": ["speaker_1"],
        "segment_start_index": 0,
        "segment_end_index": 4,
        "start_seconds": 0.0,
        "end_seconds": 60.0,
        "text": "주간 프로젝트 일정 논의",
        "summary": "일정 논의 요약",
        "metadata": '{"decision_flag": true}',
    }]]

    results = await repository.get_parent_chunks([chunk_id])

    assert len(results) == 1
    result = results[0]
    assert result.id == chunk_id
    assert result.transcript_id == transcript_id
    assert result.transcript_title == "주간 프로젝트 강의"
    assert result.topic == "일정"
    assert result.keywords == ["일정", "프로젝트"]
    assert result.speaker_labels == ["speaker_1"]
    assert result.segment_start_index == 0
    assert result.segment_end_index == 4
    assert abs(result.end_seconds - 60.0) < 1e-6
    assert result.metadata == {"decision_flag": True}
    assert result.summary == "일정 논의 요약"

    # ANY($1::uuid[]) 패턴이 SQL에 포함되는지 확인
    sql, args = connection.fetch_calls[0]
    assert "ANY" in sql
    assert "uuid[]" in sql
    assert "chunks" in sql
    assert args[0] == [chunk_id]


# --- get_transcript_by_id / summary_documents 영속화 테스트 ---


@pytest.mark.asyncio
async def test_fetch_chunks_by_transcript_parses_json_metadata() -> None:
    connection = FakeConnection()
    repository = RagRepository(connection)

    chunk_id = uuid4()
    transcript_id = uuid4()
    connection.fetch_results = [[{
        "id": chunk_id,
        "chunk_index": 0,
        "topic": "역전파",
        "subtopic": "기울기 계산",
        "keywords": ["역전파", "손실 함수"],
        "speaker_labels": [],
        "segment_start_index": 0,
        "segment_end_index": 2,
        "start_seconds": 0.0,
        "end_seconds": 45.0,
        "text": "역전파는 손실 함수의 기울기를 계산합니다.",
        "summary": "역전파의 목적을 설명합니다.",
        "metadata": '{"concepts": ["역전파"], "learning_points": ["손실 함수의 기울기를 계산한다"]}',
    }]]

    chunks = await repository.fetch_chunks_by_transcript(transcript_id)

    assert len(chunks) == 1
    assert chunks[0].id == chunk_id
    assert chunks[0].metadata == {
        "concepts": ["역전파"],
        "learning_points": ["손실 함수의 기울기를 계산한다"],
    }


class FetchrowConnection:
    """fetchrow 반환값을 테스트에서 직접 지정할 수 있는 가짜 커넥션."""

    def __init__(self, fetchrow_result) -> None:
        self.fetchrow_result = fetchrow_result
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args))
        return self.fetchrow_result

    async def execute(self, query: str, *args):
        self.calls.append((query, args))


@pytest.mark.asyncio
async def test_get_transcript_by_id_maps_row_and_filters_owner() -> None:
    transcript_id = uuid4()
    user_id = uuid4()
    connection = FetchrowConnection({
        "id": transcript_id,
        "user_id": user_id,
        "title": "주간 회의",
        "full_text": "회의 원문",
        "summary": None,
        "duration_seconds": 12.5,
        "language": "ko",
        "status": "completed",
        "created_at": None,
    })
    repository = RagRepository(connection)

    detail = await repository.get_transcript_by_id(transcript_id, user_id)

    assert detail is not None
    assert detail.id == transcript_id
    assert detail.full_text == "회의 원문"
    assert abs(detail.duration_seconds - 12.5) < 1e-6
    # user_id가 주어지면 소유권 필터(AND user_id = $2)가 SQL에 포함되고 인자로 전달됨
    sql, args = connection.calls[0]
    assert "AND user_id = $2" in sql
    assert args[1] == user_id


@pytest.mark.asyncio
async def test_get_transcript_by_id_returns_none_when_missing() -> None:
    connection = FetchrowConnection(None)
    repository = RagRepository(connection)

    detail = await repository.get_transcript_by_id(uuid4())

    assert detail is None


@pytest.mark.asyncio
async def test_get_file_detail_by_id_maps_status_and_file_fields() -> None:
    transcript_id = uuid4()
    user_id = uuid4()
    connection = FetchrowConnection({
        "id": transcript_id,
        "title": "lecture",
        "source_audio_uri": "/uploads/user/lecture.pdf",
        "original_filename": "lecture.pdf",
        "mime_type": "application/pdf",
        "source_type": "pdf",
        "status": "completed",
        "content_status": "completed",
        "index_status": "completed",
        "error_message": None,
        "duration_seconds": 12.5,
        "created_at": None,
        "updated_at": None,
    })
    repository = RagRepository(connection)

    detail = await repository.get_file_detail_by_id(transcript_id, user_id)

    assert detail is not None
    assert detail.transcript_id == transcript_id
    assert detail.file_uri == "/uploads/user/lecture.pdf"
    assert detail.source_type == "pdf"
    assert detail.content_status == "completed"
    assert detail.index_status == "completed"
    assert abs(detail.duration_seconds - 12.5) < 1e-6
    sql, args = connection.calls[0]
    assert "WHERE id = $1 AND user_id = $2" in sql
    assert "full_text" not in sql
    assert args == (transcript_id, user_id)


@pytest.mark.asyncio
async def test_list_transcripts_by_user_filters_owner_and_orders_created_at() -> None:
    connection = FakeConnection()
    repository = RagRepository(connection)
    transcript_id = uuid4()
    user_id = uuid4()
    connection.fetch_results = [[{
        "id": transcript_id,
        "title": "lecture",
        "source_audio_uri": "/uploads/user/lecture.pdf",
        "original_filename": "lecture.pdf",
        "mime_type": "application/pdf",
        "status": "completed",
        "created_at": None,
    }]]

    files = await repository.list_transcripts_by_user(user_id)

    assert len(files) == 1
    assert files[0].transcript_id == transcript_id
    assert files[0].title == "lecture"
    assert files[0].file_uri == "/uploads/user/lecture.pdf"
    assert files[0].original_filename == "lecture.pdf"
    assert files[0].mime_type == "application/pdf"
    assert files[0].status == "completed"
    sql, args = connection.fetch_calls[0]
    assert "WHERE user_id = $1" in sql
    assert "ORDER BY created_at DESC" in sql
    assert args == (user_id,)


@pytest.mark.asyncio
async def test_insert_summary_document_serializes_payload() -> None:
    document_id_holder = {}

    class InsertConnection(FetchrowConnection):
        async def fetchrow(self, query: str, *args):
            self.calls.append((query, args))
            # RETURNING id → 첫 인자(발급된 id)를 그대로 반환
            document_id_holder["id"] = args[0]
            return {"id": args[0]}

    connection = InsertConnection(None)
    repository = RagRepository(connection)

    returned_id = await repository.insert_summary_document(
        SummaryDocumentCreate(
            transcript_id=uuid4(),
            user_id=uuid4(),
            template_id="meeting_weekly",
            payload={"개요": "한글 payload", "items": ["a", "b"]},
            model="gpt-4o-mini",
        )
    )

    assert returned_id == document_id_holder["id"]
    sql, args = connection.calls[0]
    assert "INSERT INTO summary_documents" in sql
    # payload($5)는 한글 손실 없는 JSON 문자열로 직렬화됨
    assert args[4] == '{"개요": "한글 payload", "items": ["a", "b"]}'
    assert args[3] == "meeting_weekly"


@pytest.mark.asyncio
async def test_get_summary_document_parses_json_payload() -> None:
    doc_id = uuid4()
    transcript_id = uuid4()
    user_id = uuid4()
    connection = FetchrowConnection({
        "id": doc_id,
        "transcript_id": transcript_id,
        "user_id": user_id,
        "template_id": "meeting_weekly",
        # asyncpg가 JSONB를 문자열로 줄 수 있는 경우를 가정
        "payload": '{"overview": "기존 개요"}',
        "model": "gpt-4o-mini",
    })
    repository = RagRepository(connection)

    detail = await repository.get_summary_document_by_id(doc_id, user_id)

    assert detail is not None
    assert detail.template_id == "meeting_weekly"
    assert detail.payload == {"overview": "기존 개요"}


@pytest.mark.asyncio
async def test_update_summary_document_payload_returns_bool() -> None:
    # 갱신된 행이 있으면 True
    connection = FetchrowConnection({"id": uuid4()})
    repository = RagRepository(connection)
    updated = await repository.update_summary_document_payload(
        uuid4(), {"overview": "수정"}, uuid4()
    )
    assert updated is True

    # 갱신된 행이 없으면 False
    connection_missing = FetchrowConnection(None)
    repository_missing = RagRepository(connection_missing)
    not_updated = await repository_missing.update_summary_document_payload(
        uuid4(), {"overview": "수정"}
    )
    assert not_updated is False


@pytest.mark.asyncio
async def test_insert_lecture_summary_serializes_payload() -> None:
    summary_id_holder = {}

    class InsertConnection(FetchrowConnection):
        async def fetchrow(self, query: str, *args):
            self.calls.append((query, args))
            summary_id_holder["id"] = args[0]
            return {"id": args[0]}

    connection = InsertConnection(None)
    repository = RagRepository(connection)

    returned_id = await repository.insert_lecture_summary(
        LectureSummaryCreate(
            transcript_id=uuid4(),
            user_id=uuid4(),
            payload={"overview": {"summary": "강의 요약"}, "contexts": []},
            model="gpt-4o-mini",
        )
    )

    assert returned_id == summary_id_holder["id"]
    sql, args = connection.calls[0]
    assert "INSERT INTO lecture_summaries" in sql
    assert "ON CONFLICT (transcript_id)" in sql
    assert args[3] == '{"overview": {"summary": "강의 요약"}, "contexts": []}'


@pytest.mark.asyncio
async def test_get_lecture_summary_by_transcript_parses_json_payload() -> None:
    summary_id = uuid4()
    transcript_id = uuid4()
    user_id = uuid4()
    connection = FetchrowConnection({
        "id": summary_id,
        "transcript_id": transcript_id,
        "user_id": user_id,
        "payload": '{"overview": {"summary": "기존 요약"}}',
        "model": "gpt-4o-mini",
    })
    repository = RagRepository(connection)

    detail = await repository.get_lecture_summary_by_transcript(transcript_id, user_id)

    assert detail is not None
    assert detail.id == summary_id
    assert detail.payload == {"overview": {"summary": "기존 요약"}}
    sql, args = connection.calls[0]
    assert "WHERE transcript_id = $1 AND user_id = $2" in sql
    assert args == (transcript_id, user_id)


@pytest.mark.asyncio
async def test_get_parent_chunks_handles_null_optional_fields() -> None:
    # NULL 가능 필드(topic, summary, start/end_seconds 등)가 None으로 올바르게 매핑되는지 검증한다.
    connection = FakeConnection()
    repository = RagRepository(connection)

    chunk_id = uuid4()
    transcript_id = uuid4()

    connection.fetch_results = [[{
        "id": chunk_id,
        "transcript_id": transcript_id,
        "transcript_title": None,
        "chunk_index": 2,
        "topic": None,
        "subtopic": None,
        "keywords": [],
        "speaker_labels": [],
        "segment_start_index": None,
        "segment_end_index": None,
        "start_seconds": None,
        "end_seconds": None,
        "text": "강의 내용",
        "summary": None,
        "metadata": None,
    }]]

    results = await repository.get_parent_chunks([chunk_id])

    assert len(results) == 1
    result = results[0]
    assert result.topic is None
    assert result.subtopic is None
    assert result.keywords == []
    assert result.start_seconds is None
    assert result.end_seconds is None
    assert result.summary is None
    assert result.metadata == {}
