from types import SimpleNamespace

import pytest

from schemas.rag import ChunkCreate
from services.chunks.chunk_metadata_service import ChunkMetadataService


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=response),
                )
            ]
        )


def make_service(responses, concurrency: int = 2) -> ChunkMetadataService:
    service = ChunkMetadataService.__new__(ChunkMetadataService)
    service._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(responses))
    )
    service._model = "gpt-4o-mini"
    service._metadata_concurrency = concurrency
    return service


def make_chunk(
    chunk_index: int = 0,
    metadata=None,
) -> ChunkCreate:
    return ChunkCreate(
        chunk_index=chunk_index,
        chunk_strategy="lecture_test_v1",
        text="출시 일정과 담당 업무를 논의했습니다.",
        metadata=metadata
        or {
            "segment_count": 2,
            "chunk_goal": "summary_context_lecture",
            "planning_method": "llm",
            "planning_reason": "안건 단위 분리",
        },
    )


def test_chunk_metadata_prompt_targets_summary_context_metadata() -> None:
    service = make_service([])

    prompt = service._build_prompt(make_chunk())

    assert "요약자료 생성용 metadata" in prompt
    assert "검색용 metadata" not in prompt


@pytest.mark.asyncio
async def test_enrich_chunk_adds_topic_keywords_summary_and_learning_metadata() -> None:
    service = make_service(
        [
            """
            {
              "topic": "출시 일정",
              "subtopic": "",
              "keywords": ["출시", "일정", ""],
              "summary": "출시 일정과 담당 업무를 논의했습니다.",
              "metadata": {
                "concepts": ["출시 일정"],
                "learning_points": ["담당 업무를 확인한다"]
              }
            }
            """
        ]
    )

    enriched = await service.enrich_chunks([make_chunk()])

    assert enriched[0].topic == "출시 일정"
    assert enriched[0].subtopic is None
    assert enriched[0].keywords == ["출시", "일정"]
    assert enriched[0].summary == "출시 일정과 담당 업무를 논의했습니다."
    assert enriched[0].metadata["segment_count"] == 2
    assert enriched[0].metadata["chunk_goal"] == "summary_context_lecture"
    assert enriched[0].metadata["planning_method"] == "llm"
    assert enriched[0].metadata["planning_reason"] == "안건 단위 분리"
    assert enriched[0].metadata["concepts"] == ["출시 일정"]
    assert enriched[0].metadata["learning_points"] == ["담당 업무를 확인한다"]


@pytest.mark.asyncio
async def test_enrich_lecture_chunk_adds_concepts_and_learning_points() -> None:
    service = make_service(
        [
            """
            {
              "topic": "역전파",
              "subtopic": "기울기 계산",
              "keywords": ["신경망", "역전파"],
              "summary": "역전파의 목적과 계산 흐름을 설명합니다.",
              "metadata": {
                "concepts": ["역전파", "기울기"],
                "learning_points": ["손실 함수에서 가중치 방향을 계산한다"]
              }
            }
            """
        ]
    )

    enriched = await service.enrich_chunks(
        [
            make_chunk(
                metadata={
                    "segment_count": 2,
                    "chunk_goal": "summary_context_lecture",
                    "overlap_from_previous": 1,
                },
            )
        ]
    )

    assert enriched[0].topic == "역전파"
    assert enriched[0].subtopic == "기울기 계산"
    assert enriched[0].metadata["chunk_goal"] == "summary_context_lecture"
    assert enriched[0].metadata["overlap_from_previous"] == 1
    assert enriched[0].metadata["concepts"] == ["역전파", "기울기"]
    assert enriched[0].metadata["learning_points"] == ["손실 함수에서 가중치 방향을 계산한다"]


@pytest.mark.asyncio
async def test_enrich_chunk_returns_original_when_provider_or_json_fails() -> None:
    chunks = [make_chunk(chunk_index=0), make_chunk(chunk_index=1)]
    service = make_service([RuntimeError("provider down"), "not-json"])

    enriched = await service.enrich_chunks(chunks)

    assert enriched == chunks


@pytest.mark.asyncio
async def test_enrich_chunks_preserves_input_order() -> None:
    service = make_service(
        [
            '{"topic": "두 번째", "keywords": ["b"], "summary": "두 번째 요약", "metadata": {}}',
            '{"topic": "첫 번째", "keywords": ["a"], "summary": "첫 번째 요약", "metadata": {}}',
        ],
        concurrency=1,
    )

    enriched = await service.enrich_chunks(
        [make_chunk(chunk_index=1), make_chunk(chunk_index=0)]
    )

    assert [chunk.chunk_index for chunk in enriched] == [1, 0]
    assert [chunk.topic for chunk in enriched] == ["두 번째", "첫 번째"]
