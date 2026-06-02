import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.summary.pdf_templates import get_template
from services.summary.templated_summary_service import TemplatedSummaryService


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


def make_service(
    handler,
    max_input_chars: int = 48000,
    text_chunk_chars: int = 16000,
    summary_concurrency: int = 2,
):
    service = TemplatedSummaryService.__new__(TemplatedSummaryService)
    service._client = FakeClient(handler)
    service._model = "gpt-4o-mini"
    service._max_input_chars = max_input_chars
    service._text_chunk_chars = text_chunk_chars
    service._summary_concurrency = summary_concurrency
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
async def test_short_input_calls_llm_once() -> None:
    """짧은 입력은 단일 LLM 호출(reduce 경로)만 발생해야 한다."""
    template = get_template("meeting_weekly")
    call_count = 0

    async def handler(**kwargs):
        nonlocal call_count
        call_count += 1
        return make_response(json.dumps({"overview": "요약"}, ensure_ascii=False))

    service = make_service(handler, max_input_chars=10000)
    short_text = "짧은 회의 녹취록 내용"
    await service.summarize_for_template(short_text, template)

    assert call_count == 1


@pytest.mark.asyncio
async def test_long_input_map_reduce_call_counts() -> None:
    """긴 입력은 윈도우 수만큼 map 호출 + reduce 1회가 발생해야 한다."""
    template = get_template("meeting_weekly")
    # map 호출: response_format 없음, reduce 호출: response_format=json_object
    map_calls = 0
    reduce_calls = 0

    async def handler(**kwargs):
        nonlocal map_calls, reduce_calls
        if kwargs.get("response_format") == {"type": "json_object"}:
            reduce_calls += 1
            return make_response(json.dumps({"overview": "통합요약"}, ensure_ascii=False))
        else:
            map_calls += 1
            return make_response("부분요약 내용")

    # max_input_chars=200, chunk_chars=50 → "X"*400은 8개 윈도우
    # 부분요약 묶음(~126자)은 200 미만이므로 2차 map 패스 없이 reduce 1회만 발생
    service = make_service(handler, max_input_chars=200, text_chunk_chars=50)
    long_text = "X" * 400
    await service.summarize_for_template(long_text, template)

    # 윈도우 수(400/50=8)만큼 map, reduce는 정확히 1회
    assert map_calls == 8
    assert reduce_calls == 1


@pytest.mark.asyncio
async def test_long_input_full_content_preserved() -> None:
    """긴 입력의 전체 내용이 map 단계에 빠짐없이 전달되어야 한다(잘림 없음)."""
    template = get_template("meeting_weekly")
    total_markers_in_map = 0

    async def handler(**kwargs):
        nonlocal total_markers_in_map
        if kwargs.get("response_format") == {"type": "json_object"}:
            return make_response(json.dumps({"overview": "통합요약"}, ensure_ascii=False))
        # map 호출: 프롬프트에 포함된 마커 문자 'X' 수 누적
        content = kwargs["messages"][1]["content"]
        total_markers_in_map += content.count("X")
        return make_response("부분요약")

    service = make_service(handler, max_input_chars=100, text_chunk_chars=50)
    marker_count = 400
    long_text = "X" * marker_count
    await service.summarize_for_template(long_text, template)

    # 모든 마커 문자가 map 프롬프트에 전달되어야 함 — 잘림이 없어야 한다
    assert total_markers_in_map == marker_count


@pytest.mark.asyncio
async def test_long_input_concurrency_limit() -> None:
    """동시 실행 수가 summary_concurrency를 초과하지 않아야 한다."""
    template = get_template("meeting_weekly")
    concurrency_limit = 2
    current_concurrent = 0
    max_concurrent_seen = 0

    async def handler(**kwargs):
        nonlocal current_concurrent, max_concurrent_seen
        if kwargs.get("response_format") == {"type": "json_object"}:
            return make_response(json.dumps({"overview": "요약"}, ensure_ascii=False))
        # map 핸들러: 동시 실행 수 추적
        current_concurrent += 1
        max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
        await asyncio.sleep(0)  # 다른 태스크에 실행 기회 양보
        current_concurrent -= 1
        return make_response("부분요약")

    service = make_service(
        handler,
        max_input_chars=100,
        text_chunk_chars=50,
        summary_concurrency=concurrency_limit,
    )
    long_text = "X" * 400
    await service.summarize_for_template(long_text, template)

    assert max_concurrent_seen <= concurrency_limit


@pytest.mark.asyncio
async def test_reduce_result_normalized_by_template() -> None:
    """reduce 결과가 템플릿 섹션 key 기준으로 정규화되어야 한다."""
    template = get_template("meeting_weekly")

    async def handler(**kwargs):
        if kwargs.get("response_format") == {"type": "json_object"}:
            # 일부 섹션만 반환
            return make_response(json.dumps({"overview": "요약"}, ensure_ascii=False))
        return make_response("부분요약")

    service = make_service(handler, max_input_chars=100, text_chunk_chars=50)
    result = await service.summarize_for_template("X" * 400, template)

    # 모든 템플릿 섹션 key가 결과에 포함되어야 한다
    assert set(result.keys()) == {s.key for s in template.sections}
    # 누락된 섹션은 빈 문자열로 보정
    assert result["overview"] == "요약"
    for key in {s.key for s in template.sections} - {"overview"}:
        assert result[key] == "" or result[key] == []


@pytest.mark.asyncio
async def test_summarize_raises_on_invalid_json() -> None:
    template = get_template("meeting_weekly")

    async def handler(**kwargs):
        return make_response("이건 JSON이 아닙니다")

    service = make_service(handler)
    with pytest.raises(HTTPException) as exc:
        await service.summarize_for_template("회의 원문", template)

    assert exc.value.status_code == 502
