from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from db import connection as db_connection
from repositories.rag_repository import RagRepository
from schemas.rag import (
    ChunkCreate,
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
