import pytest

from schemas.rag import SegmentCreate
from services.context_chunk_planning_service import (
    ContextChunkPlanGroup,
    ContextChunkPlanningService,
)


def make_segment(index: int, text: str) -> SegmentCreate:
    return SegmentCreate(
        segment_index=index,
        start_seconds=float(index * 60),
        end_seconds=float(index * 60 + 40),
        text=text,
    )


def make_service(response: str | Exception) -> ContextChunkPlanningService:
    service = ContextChunkPlanningService.__new__(ContextChunkPlanningService)
    service._meeting_fallback_max_seconds = 180.0
    service._lecture_fallback_max_seconds = 300.0

    async def fake_request_plan(domain_type, segments):
        if isinstance(response, Exception):
            raise response
        return response

    service._request_plan = fake_request_plan
    return service


def test_context_chunk_planner_prompt_contains_domain_and_common_rules() -> None:
    service = make_service("{}")

    meeting_prompt = service._build_prompt("meeting", [make_segment(0, "첫 번째 안건입니다.")])
    lecture_prompt = service._build_prompt("lecture", [make_segment(0, "첫 번째 개념입니다.")])

    assert "하나의 안건" in meeting_prompt
    assert "하나의 개념" in lecture_prompt
    assert "segment_end_index + 1" in meeting_prompt
    assert "너무 짧은 chunk" in meeting_prompt
    assert "의미가 덜 깨지는 지점" in lecture_prompt


@pytest.mark.asyncio
async def test_context_chunk_planner_parses_meeting_groups() -> None:
    service = make_service(
        """
        {
          "groups": [
            {
              "segment_start_index": 0,
              "segment_end_index": 1,
              "topic": "출시 일정",
              "reason": "출시 일정 논의",
              "summary_hint": "출시 시점을 논의함"
            },
            {
              "segment_start_index": 2,
              "segment_end_index": 3,
              "topic": "담당 업무",
              "reason": "액션 아이템 논의",
              "summary_hint": "담당자를 정리함"
            }
          ]
        }
        """
    )

    groups = await service.plan_chunks(
        "meeting",
        [
            make_segment(0, "출시 일정을 확인합니다."),
            make_segment(1, "다음 주가 적절합니다."),
            make_segment(2, "테스트 담당자를 정해야 합니다."),
            make_segment(3, "Mina가 맡겠습니다."),
        ],
    )

    assert groups == [
        ContextChunkPlanGroup(0, 1, "출시 일정", "출시 일정 논의", "출시 시점을 논의함"),
        ContextChunkPlanGroup(2, 3, "담당 업무", "액션 아이템 논의", "담당자를 정리함"),
    ]


@pytest.mark.asyncio
async def test_context_chunk_planner_parses_lecture_groups() -> None:
    service = make_service(
        """
        {
          "groups": [
            {
              "segment_start_index": 0,
              "segment_end_index": 2,
              "topic": "역전파",
              "reason": "하나의 개념 설명",
              "summary_hint": "역전파 정의와 예시"
            }
          ]
        }
        """
    )

    groups = await service.plan_chunks(
        "lecture",
        [
            make_segment(0, "역전파를 설명합니다."),
            make_segment(1, "손실 함수에서 기울기를 계산합니다."),
            make_segment(2, "예시를 보겠습니다."),
        ],
    )

    assert len(groups) == 1
    assert groups[0].segment_start_index == 0
    assert groups[0].segment_end_index == 2
    assert groups[0].topic == "역전파"


@pytest.mark.asyncio
async def test_context_chunk_planner_falls_back_when_json_is_invalid() -> None:
    service = make_service("not-json")

    groups = await service.plan_chunks(
        "meeting",
        [
            make_segment(0, "첫 번째 안건입니다."),
            make_segment(1, "계속 같은 안건입니다."),
            make_segment(2, "다음 안건입니다."),
            make_segment(3, "마무리합니다."),
        ],
    )

    assert [group.segment_start_index for group in groups] == [0, 3]
    assert [group.segment_end_index for group in groups] == [2, 3]
    assert all("fallback" in group.reason for group in groups)


@pytest.mark.asyncio
async def test_context_chunk_planner_falls_back_when_range_is_invalid() -> None:
    service = make_service(
        """
        {
          "groups": [
            {"segment_start_index": 0, "segment_end_index": 0, "reason": "시작"},
            {"segment_start_index": 2, "segment_end_index": 2, "reason": "누락"}
          ]
        }
        """
    )

    groups = await service.plan_chunks(
        "lecture",
        [
            make_segment(0, "개념 하나."),
            make_segment(1, "누락되면 안 됩니다."),
            make_segment(2, "개념 둘."),
        ],
    )

    assert groups[0].segment_start_index == 0
    assert groups[0].segment_end_index == 2
    assert groups[0].reason == "LLM planner fallback: 300초 안전 기준"
    assert groups[0].planning_method == "fallback"
    assert "Context chunk groups must be adjacent" in groups[0].planning_error
    assert "missing_indices=[1]" in groups[0].planning_error
    assert "planned_ranges=[(0, 0), (2, 2)]" in groups[0].planning_error
