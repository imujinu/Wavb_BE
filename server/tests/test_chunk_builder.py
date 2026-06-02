from schemas.rag import SegmentCreate
from services.chunks.chunk_builder import (
    ContextPlannedChunkBuilder,
    DeterministicFallbackChunkBuilder,
    LectureChunkBuilder,
    MeetingChunkBuilder,
    get_chunk_builder,
)
from services.chunks.context_chunk_planning_service import ContextChunkPlanGroup


def make_segment(
    index: int,
    text: str,
    speaker_label: str | None = None,
) -> SegmentCreate:
    return SegmentCreate(
        segment_index=index,
        start_seconds=float(index * 10),
        end_seconds=float(index * 10 + 8),
        text=text,
        speaker_label=speaker_label,
    )


def test_meeting_chunk_builder_splits_on_speaker_turns() -> None:
    builder = MeetingChunkBuilder()
    chunks = builder.build(
        [
            make_segment(0, "We reviewed the launch date.", "speaker_1"),
            make_segment(1, "The action item is assigned to Mina.", "speaker_1"),
            make_segment(2, "I confirmed the budget decision.", "speaker_2"),
        ]
    )

    assert len(chunks) == 2
    assert chunks[0].domain_type == "meeting"
    assert chunks[0].chunk_strategy == "meeting_speaker_turn_v1"
    assert chunks[0].segment_start_index == 0
    assert chunks[0].segment_end_index == 1
    assert chunks[0].speaker_labels == ["speaker_1"]
    assert chunks[0].metadata["chunk_goal"] == "summary_context_meeting"
    assert chunks[0].metadata["planning_method"] == "fallback"
    assert chunks[1].segment_start_index == 2
    assert chunks[1].speaker_labels == ["speaker_2"]


def test_lecture_chunk_builder_uses_larger_context_with_overlap() -> None:
    builder = LectureChunkBuilder(max_tokens=140, overlap_segments=1)
    long_text = "concept explanation " * 8
    chunks = builder.build(
        [
            make_segment(0, long_text),
            make_segment(1, long_text),
            make_segment(2, long_text),
        ]
    )

    assert len(chunks) == 2
    assert chunks[0].domain_type == "lecture"
    assert chunks[0].chunk_strategy == "lecture_context_section_v1"
    assert chunks[0].segment_start_index == 0
    assert chunks[0].segment_end_index == 1
    assert chunks[0].metadata["overlap_from_previous"] == 0
    assert chunks[0].metadata["chunk_goal"] == "summary_context_lecture"
    assert chunks[0].metadata["planning_method"] == "fallback"
    assert chunks[1].segment_start_index == 1
    assert chunks[1].segment_end_index == 2
    assert chunks[1].metadata["overlap_from_previous"] == 1


def test_context_planned_chunk_builder_preserves_llm_plan_range_and_metadata() -> None:
    builder = ContextPlannedChunkBuilder()

    chunks = builder.build(
        "meeting",
        [
            make_segment(0, "출시 일정을 확인합니다.", "speaker_1"),
            make_segment(1, "다음 주가 적절합니다.", "speaker_2"),
            make_segment(2, "담당자는 Mina입니다.", "speaker_1"),
        ],
        [
            ContextChunkPlanGroup(
                0,
                1,
                "출시 일정",
                "하나의 안건 논의",
                "출시 시점을 정리합니다.",
            ),
            ContextChunkPlanGroup(2, 2, "담당 업무", "액션 아이템 논의", None),
        ],
    )

    assert len(chunks) == 2
    assert chunks[0].chunk_strategy == "meeting_context_plan_v1"
    assert chunks[0].segment_start_index == 0
    assert chunks[0].segment_end_index == 1
    assert chunks[0].topic is None
    assert chunks[0].metadata["planning_method"] == "llm"
    assert chunks[0].metadata["planning_topic"] == "출시 일정"
    assert chunks[0].metadata["planning_reason"] == "하나의 안건 논의"
    assert chunks[0].metadata["summary_hint"] == "출시 시점을 정리합니다."
    assert chunks[0].metadata["segment_count"] == 2
    assert chunks[1].segment_start_index == 2
    assert chunks[1].segment_end_index == 2


def test_context_planned_chunk_builder_preserves_fallback_planning_error() -> None:
    builder = ContextPlannedChunkBuilder()

    chunks = builder.build(
        "lecture",
        [make_segment(0, "첫 번째 개념입니다.")],
        [
            ContextChunkPlanGroup(
                0,
                0,
                None,
                "LLM planner fallback: 300초 안전 기준",
                None,
                "fallback",
                "ValueError: Context chunk groups must be adjacent.",
            )
        ],
    )

    assert chunks[0].chunk_strategy == "lecture_context_fallback_v1"
    assert chunks[0].metadata["planning_method"] == "fallback"
    assert chunks[0].metadata["planning_error"] == (
        "ValueError: Context chunk groups must be adjacent."
    )


def test_deterministic_fallback_chunk_builder_uses_safety_time_ranges() -> None:
    builder = DeterministicFallbackChunkBuilder(meeting_max_seconds=180.0)

    chunks = builder.build(
        "meeting",
        [
            SegmentCreate(
                segment_index=0,
                start_seconds=0.0,
                end_seconds=60.0,
                text="첫 번째 안건입니다.",
            ),
            SegmentCreate(
                segment_index=1,
                start_seconds=70.0,
                end_seconds=120.0,
                text="같은 안건을 계속 설명합니다.",
            ),
            SegmentCreate(
                segment_index=2,
                start_seconds=130.0,
                end_seconds=200.0,
                text="다음 안건입니다.",
            ),
            SegmentCreate(
                segment_index=3,
                start_seconds=210.0,
                end_seconds=250.0,
                text="마무리입니다.",
            ),
        ],
    )

    assert [chunk.segment_start_index for chunk in chunks] == [0, 2]
    assert [chunk.segment_end_index for chunk in chunks] == [1, 3]
    assert chunks[0].chunk_strategy == "meeting_context_fallback_v1"
    assert chunks[0].metadata["planning_method"] == "fallback"
    assert "180초" in chunks[0].metadata["planning_reason"]


def test_get_chunk_builder_selects_domain_strategy() -> None:
    assert isinstance(get_chunk_builder("meeting"), MeetingChunkBuilder)
    assert isinstance(get_chunk_builder("lecture"), LectureChunkBuilder)
