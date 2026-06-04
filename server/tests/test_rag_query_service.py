from uuid import UUID, uuid4

import pytest

from schemas.rag import ParentChunkResult, SearchChunkHit
from services.rag.rag_query_service import RagQueryService


class FakeRepository:
    def __init__(self) -> None:
        self.search_calls = []
        self.parent_calls = []
        self.parent_chunks: list[ParentChunkResult] = []
        self.hits: list[SearchChunkHit] = []

    async def search_chunks_hybrid(
        self,
        morpheme_query,
        embedding,
        transcript_ids,
        user_id,
        top_k,
    ):
        self.search_calls.append(
            {
                "morpheme_query": morpheme_query,
                "embedding": embedding,
                "transcript_ids": transcript_ids,
                "user_id": user_id,
                "top_k": top_k,
            }
        )
        return self.hits

    async def get_parent_chunks(self, parent_chunk_ids):
        self.parent_calls.append(parent_chunk_ids)
        return self.parent_chunks


class FakeEmbeddingService:
    async def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeMorphemeService:
    def tokenize(self, text):
        return f"morpheme:{text}"


def make_service(repository: FakeRepository) -> RagQueryService:
    return RagQueryService(
        repository=repository,
        embedding_service=FakeEmbeddingService(),
        morpheme_service=FakeMorphemeService(),
    )


@pytest.mark.asyncio
async def test_search_uses_transcript_ids_and_returns_retrieved_sources() -> None:
    repository = FakeRepository()
    transcript_id = uuid4()
    parent_id = uuid4()
    user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    repository.hits = [
        SearchChunkHit(
            id=uuid4(),
            transcript_id=transcript_id,
            parent_chunk_id=parent_id,
            child_index=0,
            start_seconds=0.0,
            end_seconds=30.0,
            text="검색 child",
            score=0.25,
        )
    ]
    repository.parent_chunks = [
        ParentChunkResult(
            id=parent_id,
            transcript_id=transcript_id,
            transcript_title="딥러닝 강의",
            chunk_index=3,
            topic="역전파",
            subtopic=None,
            keywords=["역전파", "기울기"],
            speaker_labels=[],
            segment_start_index=4,
            segment_end_index=8,
            start_seconds=40.0,
            end_seconds=80.0,
            text="역전파는 손실 함수의 기울기를 계산합니다.",
            summary="역전파의 목적과 계산 흐름을 설명합니다.",
            metadata={},
        )
    ]
    service = make_service(repository)

    sources = await service.search(
        query="역전파가 뭐야?",
        transcript_ids=[transcript_id],
        user_id=user_id,
        top_k=5,
    )

    assert repository.search_calls[0]["morpheme_query"] == "morpheme:역전파가 뭐야?"
    assert repository.search_calls[0]["transcript_ids"] == [transcript_id]
    assert repository.search_calls[0]["user_id"] == user_id
    assert repository.parent_calls[0] == [parent_id]
    assert len(sources) == 1
    assert sources[0].source_type == "document"
    assert sources[0].title == "딥러닝 강의"
    assert sources[0].snippet == "역전파의 목적과 계산 흐름을 설명합니다."
    assert sources[0].score == 0.25
    assert sources[0].metadata["topic"] == "역전파"
    assert sources[0].metadata["segment_start_index"] == 4


@pytest.mark.asyncio
async def test_search_returns_empty_sources_when_no_hits() -> None:
    repository = FakeRepository()
    service = make_service(repository)
    transcript_id = uuid4()
    user_id = uuid4()

    sources = await service.search(
        query="없는 내용",
        transcript_ids=[transcript_id],
        user_id=user_id,
        top_k=5,
    )

    assert sources == []
    assert repository.parent_calls == []
