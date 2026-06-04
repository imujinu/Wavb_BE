from types import SimpleNamespace
from uuid import uuid4

import pytest

from schemas.rag import RetrievedSource
from services.rag.rag_response_service import RagResponseService


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def make_service(content: str = "답변입니다.") -> RagResponseService:
    service = RagResponseService.__new__(RagResponseService)
    completions = FakeCompletions(content)
    service._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    service._model = "gpt-4o-mini"
    service._fake_completions = completions
    return service


def make_source() -> RetrievedSource:
    return RetrievedSource(
        source_type="document",
        title="딥러닝 강의",
        snippet="역전파는 손실 함수의 기울기를 계산합니다.",
        transcript_id=uuid4(),
        score=0.5,
        metadata={"topic": "역전파", "keywords": ["기울기"]},
    )


@pytest.mark.asyncio
async def test_generate_returns_message_without_sources() -> None:
    service = make_service()

    answer = await service.generate("없는 내용", [])

    assert answer == "제공된 강의 자료에는 해당 내용이 없습니다."
    assert service._fake_completions.calls == []


@pytest.mark.asyncio
async def test_generate_builds_context_from_retrieved_sources() -> None:
    service = make_service("역전파는 기울기 계산 과정입니다.")

    answer = await service.generate("역전파가 뭐야?", [make_source()])

    assert answer == "역전파는 기울기 계산 과정입니다."
    prompt = service._fake_completions.calls[0]["messages"][1]["content"]
    assert "source_type: document" in prompt
    assert "title: 딥러닝 강의" in prompt
    assert "topic: 역전파" in prompt
    assert "역전파는 손실 함수의 기울기를 계산합니다." in prompt
    system_prompt = service._fake_completions.calls[0]["messages"][0]["content"]
    assert "title, topic, keywords, snippet" in system_prompt
