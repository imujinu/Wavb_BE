import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.pdf_templates import get_template
from services.templated_summary_service import TemplatedSummaryService


# test_summary_service.py와 동일한 Fake OpenAI client 패턴을 재사용한다.
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
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def make_service(handler, max_input_chars: int = 48000):
    service = TemplatedSummaryService.__new__(TemplatedSummaryService)
    service._client = FakeClient(handler)
    service._model = "gpt-4o-mini"
    service._max_input_chars = max_input_chars
    return service


@pytest.mark.asyncio
async def test_summarize_maps_template_sections() -> None:
    template = get_template("meeting_weekly")
    # 모델이 섹션 key에 맞춰 JSON을 반환하는 상황을 가정
    payload = {
        "overview": " 주간 회의 개요 ",
        "agenda": ["안건1", "안건2"],
        "discussion": "논의 내용",
        "decisions": ["결정1"],
        "action_items": [],
    }

    async def handler(**kwargs):
        return make_response(json.dumps(payload, ensure_ascii=False))

    service = make_service(handler)
    result = await service.summarize_for_template("회의 원문", template, title="주간 회의")

    # 모든 섹션 key가 채워지고, 문자열은 strip, 리스트는 공백 제거됨
    assert set(result.keys()) == {s.key for s in template.sections}
    assert result["overview"] == "주간 회의 개요"
    assert result["agenda"] == ["안건1", "안건2"]
    assert result["action_items"] == []


@pytest.mark.asyncio
async def test_summarize_fills_missing_sections_with_empty() -> None:
    template = get_template("lecture_general")

    async def handler(**kwargs):
        # topic만 반환하고 나머지 섹션은 누락
        return make_response(json.dumps({"topic": "강의 주제"}, ensure_ascii=False))

    service = make_service(handler)
    result = await service.summarize_for_template("강의 원문", template)

    assert result["topic"] == "강의 주제"
    # 누락 섹션은 빈 문자열로 보정
    assert result["key_points"] == ""
    assert result["concepts"] == ""
    assert result["keywords"] == ""


@pytest.mark.asyncio
async def test_summarize_rejects_empty_transcript() -> None:
    template = get_template("meeting_weekly")

    async def handler(**kwargs):
        raise AssertionError("LLM should not be called for empty transcript")

    service = make_service(handler)
    with pytest.raises(HTTPException) as exc:
        await service.summarize_for_template("   ", template)

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_summarize_truncates_long_input() -> None:
    template = get_template("meeting_weekly")
    captured = {}

    async def handler(**kwargs):
        captured["content"] = kwargs["messages"][1]["content"]
        return make_response(json.dumps({"overview": "요약"}, ensure_ascii=False))

    # 입력 상한을 100자로 낮추고, 그보다 긴 원문을 전달
    # (프롬프트 boilerplate에 없는 마커 문자 'X'를 사용해 원문 길이만 정확히 측정)
    service = make_service(handler, max_input_chars=100)
    long_text = "X" * 5000
    await service.summarize_for_template(long_text, template)

    # 프롬프트에 포함된 원문이 상한(100자) 이하로 잘렸는지 검증 (마커 문자 수로 확인)
    assert captured["content"].count("X") == 100


@pytest.mark.asyncio
async def test_summarize_raises_on_invalid_json() -> None:
    template = get_template("meeting_weekly")

    async def handler(**kwargs):
        return make_response("이건 JSON이 아닙니다")

    service = make_service(handler)
    with pytest.raises(HTTPException) as exc:
        await service.summarize_for_template("회의 원문", template)

    assert exc.value.status_code == 502
