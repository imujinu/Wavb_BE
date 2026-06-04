import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from schemas.rag import ChunkRow, LectureSummaryDetail, TranscriptDetail
from services.summary.lecture_summary_service import LectureSummaryService


FAKE_USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class FakeCompletions:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=self.response))
            ]
        )


class FakeRepository:
    def __init__(
        self,
        transcript: TranscriptDetail | None,
        chunks: list[ChunkRow] | None = None,
        existing_summary: LectureSummaryDetail | None = None,
    ) -> None:
        self.transcript = transcript
        self.chunks = chunks or []
        self.existing_summary = existing_summary
        self.inserted_summary = None
        self.inserted_id = uuid4()

    async def get_transcript_by_id(self, transcript_id, user_id=None):
        return self.transcript

    async def get_lecture_summary_by_transcript(self, transcript_id, user_id=None):
        return self.existing_summary

    async def fetch_chunks_by_transcript(self, transcript_id):
        return self.chunks

    async def insert_lecture_summary(self, summary):
        self.inserted_summary = summary
        return self.inserted_id


def make_service(repository: FakeRepository, response: str = "{}") -> LectureSummaryService:
    service = LectureSummaryService.__new__(LectureSummaryService)
    completions = FakeCompletions(response)
    service._repository = repository
    service._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    service._model = "gpt-4o-mini"
    service._fake_completions = completions
    return service


def make_transcript(
    status: str = "completed",
    full_text: str | None = "강의 원문",
) -> TranscriptDetail:
    return TranscriptDetail(
        id=uuid4(),
        user_id=FAKE_USER_ID,
        title="딥러닝 강의",
        full_text=full_text,
        summary=None,
        duration_seconds=120.0,
        language="ko",
        status=status,
    )


def make_chunk() -> ChunkRow:
    return ChunkRow(
        id=uuid4(),
        chunk_index=0,
        topic="역전파",
        subtopic=None,
        keywords=["역전파", "기울기"],
        speaker_labels=[],
        segment_start_index=0,
        segment_end_index=2,
        start_seconds=0.0,
        end_seconds=120.0,
        text="역전파와 기울기 계산을 설명합니다.",
        summary="역전파의 목적을 설명합니다.",
        metadata={
            "concepts": ["역전파"],
            "learning_points": ["손실 함수에서 기울기를 계산한다"],
        },
    )


@pytest.mark.asyncio
async def test_get_or_create_returns_existing_summary_without_llm_call() -> None:
    transcript = make_transcript()
    existing = LectureSummaryDetail(
        id=uuid4(),
        transcript_id=transcript.id,
        user_id=FAKE_USER_ID,
        payload={"overview": {"title": "기존", "summary": "기존 요약"}},
        model="gpt-4o-mini",
    )
    repository = FakeRepository(transcript, existing_summary=existing)
    service = make_service(repository)

    result = await service.get_or_create_summary(transcript.id, FAKE_USER_ID)

    assert result.summary_id == existing.id
    assert result.persona_id == "general"
    assert result.overview.summary == "기존 요약"
    assert service._fake_completions.calls == []
    assert repository.inserted_summary is None


@pytest.mark.asyncio
async def test_get_or_create_generates_normalizes_and_saves_payload() -> None:
    transcript = make_transcript()
    chunk = make_chunk()
    response = json.dumps(
        {
            "overview": {
                "title": "딥러닝 강의",
                "summary": "역전파를 설명했습니다.",
                "key_points": ["기울기 계산"],
            },
            "contexts": [
                {
                    "index": 0,
                    "topic": "역전파",
                    "subtitle": "역전파",
                    "content": "역전파의 목적과 계산 흐름",
                    "keywords": ["역전파"],
                    "concepts": ["역전파"],
                    "learning_points": ["기울기 계산"],
                    "start_seconds": 0,
                    "end_seconds": 120,
                    "segment_start_index": 0,
                    "segment_end_index": 2,
                }
            ],
            "keywords": [
                {
                    "keyword": "역전파",
                    "summary": "기울기 계산 방법으로 다뤄졌습니다.",
                    "related_context_indices": [0],
                }
            ],
        },
        ensure_ascii=False,
    )
    repository = FakeRepository(transcript, chunks=[chunk])
    service = make_service(repository, response)

    result = await service.get_or_create_summary(transcript.id, FAKE_USER_ID)

    assert result.summary_id == repository.inserted_id
    assert result.overview.key_points == ["기울기 계산"]
    assert result.contexts[0].topic == "역전파"
    assert result.contexts[0].keywords == ["역전파"]
    assert result.contexts[0].concepts == ["역전파"]
    assert result.contexts[0].learning_points == ["기울기 계산"]
    assert result.contexts[0].segment_end_index == 2
    assert result.keywords[0].related_context_indices == [0]
    assert repository.inserted_summary is not None
    assert repository.inserted_summary.payload == {
        "overview": result.overview.model_dump(),
        "contexts": [context.model_dump() for context in result.contexts],
        "keywords": [keyword.model_dump() for keyword in result.keywords],
    }


@pytest.mark.asyncio
async def test_get_or_create_rejects_missing_or_unready_transcript() -> None:
    repository = FakeRepository(None)
    service = make_service(repository)

    with pytest.raises(HTTPException) as missing_exc:
        await service.get_or_create_summary(uuid4(), FAKE_USER_ID)
    assert missing_exc.value.status_code == 404

    processing_repo = FakeRepository(make_transcript(status="processing"))
    processing_service = make_service(processing_repo)
    with pytest.raises(HTTPException) as processing_exc:
        await processing_service.get_or_create_summary(uuid4(), FAKE_USER_ID)
    assert processing_exc.value.status_code == 409

    empty_repo = FakeRepository(make_transcript(full_text="   "))
    empty_service = make_service(empty_repo)
    with pytest.raises(HTTPException) as empty_exc:
        await empty_service.get_or_create_summary(uuid4(), FAKE_USER_ID)
    assert empty_exc.value.status_code == 409


@pytest.mark.asyncio
async def test_get_or_create_rejects_when_chunks_are_missing() -> None:
    transcript = make_transcript()
    repository = FakeRepository(transcript, chunks=[])
    service = make_service(repository)

    with pytest.raises(HTTPException) as exc:
        await service.get_or_create_summary(transcript.id, FAKE_USER_ID)

    assert exc.value.status_code == 409
    assert exc.value.detail == "Transcript chunks are not ready."
