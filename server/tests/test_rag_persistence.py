from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from db import connection as db_connection
from repositories.rag_repository import RagRepository
from schemas.rag import (
    ChunkCreate,
    SearchChunkCreate,
    SegmentCreate,
    TranscriptCreate,
    TranscriptResultUpdate,
)
from settings import Settings


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    async def fetchrow(self, query: str, *args):
        self.executed.append((query, args))
        return {"id": args[0]}

    async def execute(self, query: str, *args):
        self.executed.append((query, args))

    async def executemany(self, query: str, args: list[tuple]):
        self.executemany_calls.append((query, args))


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
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "embedding vector(1536)" in migration
    assert "idx_chunks_text_fts" in migration
    assert "idx_chunks_embedding" in migration


def test_pydantic_models_validate_domain_and_ranges() -> None:
    with pytest.raises(ValidationError):
        TranscriptCreate(domain_type="memo", source_audio_uri="uploads/a.mp3")

    with pytest.raises(ValidationError):
        SegmentCreate(
            segment_index=0,
            start_seconds=10,
            end_seconds=9,
            text="invalid range",
        )

    with pytest.raises(ValidationError):
        ChunkCreate(
            domain_type="meeting",
            chunk_index=0,
            chunk_strategy="meeting_v1",
            text="invalid range",
            segment_start_index=2,
            segment_end_index=1,
        )


def test_chunk_model_normalizes_blank_metadata_lists() -> None:
    chunk = ChunkCreate(
        domain_type="lecture",
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
            domain_type="meeting",
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
    assert connection.executed[0][1][2] == "meeting"
    assert connection.executed[0][1][4] == "uploads/meeting.mp3"
    assert "UPDATE transcripts" in connection.executed[1][0]
    assert connection.executed[1][1][1] == "회의 내용"
    assert connection.executed[1][1][5] == "completed"


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
            )
        ],
    )
    await repository.insert_chunks(
        transcript_id,
        [
            ChunkCreate(
                domain_type="meeting",
                chunk_index=0,
                chunk_strategy="meeting_speaker_turn_v1",
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
            )
        ],
    )

    assert "INSERT INTO segments" in connection.executemany_calls[0][0]
    assert connection.executemany_calls[0][1][0][1] == transcript_id
    assert connection.executemany_calls[0][1][0][8] == '{"provider": "openai"}'
    assert "INSERT INTO chunks" in connection.executemany_calls[1][0]
    assert connection.executemany_calls[1][1][0][2] == "meeting"
    assert connection.executemany_calls[1][1][0][13] == ["일정"]
    assert connection.executemany_calls[1][1][0][17] == "[0.1,0.2,0.3]"


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
    # index 9: text_morphemes (None — 테스트에서 형태소 서비스 미주입)
    assert first_row[9] is None
    # index 10: embedding_model
    assert first_row[10] == "text-embedding-3-small"

    # 두 번째 행도 정상 저장되는지 확인
    assert params[1][3] == 1
    assert params[1][8] == "두 번째 검색 청크"


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
