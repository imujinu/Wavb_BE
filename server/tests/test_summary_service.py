import asyncio
from types import SimpleNamespace

import pytest

from services.summary_service import SummaryService


class FakeCompletions:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class FakeChat:
    def __init__(self, handler):
        self.completions = FakeCompletions(handler)


class FakeClient:
    def __init__(self, handler):
        self.chat = FakeChat(handler)


def make_response(content: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
            )
        ]
    )


def make_service(handler, chunk_chars: int = 16000, concurrency: int = 2):
    service = SummaryService.__new__(SummaryService)
    service._client = FakeClient(handler)
    service._model = "gpt-4o-mini"
    service._text_chunk_chars = chunk_chars
    service._summary_concurrency = concurrency
    return service


@pytest.mark.asyncio
async def test_summarize_uses_single_summary_for_short_transcript() -> None:
    calls = []

    async def handler(**kwargs):
        calls.append(kwargs)
        return make_response("짧은 요약")

    service = make_service(handler, chunk_chars=100)

    summary = await service.summarize("짧은 회의 기록")

    assert summary == "짧은 요약"
    assert len(calls) == 1
    assert "짧은 회의 기록" in calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_summarize_chunks_long_transcript_then_creates_final_summary() -> None:
    active_count = 0
    max_active_count = 0
    calls = []

    async def handler(**kwargs):
        nonlocal active_count, max_active_count
        content = kwargs["messages"][1]["content"]
        calls.append(content)

        if "Partial summary" in content:
            return make_response("최종 요약")

        active_count += 1
        max_active_count = max(max_active_count, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        return make_response(f"부분 요약 {len(calls)}")

    service = make_service(handler, chunk_chars=10, concurrency=2)

    summary = await service.summarize("1234567890 1234567890 1234567890")

    assert summary == "최종 요약"
    assert len(calls) == 4
    assert max_active_count <= 2
    assert "Partial summary 1" in calls[-1]
    assert "Partial summary 2" in calls[-1]
    assert "Partial summary 3" in calls[-1]


def test_split_text_prefers_boundaries() -> None:
    service = SummaryService.__new__(SummaryService)

    chunks = service._split_text("alpha beta\ngamma delta", max_chars=12)

    assert chunks == ["alpha beta", "gamma delta"]
