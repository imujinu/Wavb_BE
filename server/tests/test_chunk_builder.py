from schemas.rag import SegmentCreate
from services.chunks.chunk_builder import (
    ContextPlannedChunkBuilder,
    DeterministicFallbackChunkBuilder,
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


def test_context_planned_chunk_builder_preserves_llm_plan_range_and_metadata() -> None:
    builder = ContextPlannedChunkBuilder()

    chunks = builder.build(
        [
            make_segment(0, "역전파를 설명합니다."),
            make_segment(1, "손실 함수에서 기울기를 계산합니다."),
            make_segment(2, "예시를 보겠습니다."),
        ],
        [
            ContextChunkPlanGroup(
                0,
                1,
                "역전파",
                "하나의 개념 설명",
                "역전파 정의를 정리합니다.",
            ),
            ContextChunkPlanGroup(2, 2, "예시", "예시 흐름", None),
        ],
    )

    assert len(chunks) == 2
    assert chunks[0].chunk_strategy == "lecture_context_plan_v1"
    assert chunks[0].segment_start_index == 0
    assert chunks[0].segment_end_index == 1
    assert chunks[0].topic is None
    assert chunks[0].metadata["chunk_goal"] == "summary_context_lecture"
    assert chunks[0].metadata["planning_method"] == "llm"
    assert chunks[0].metadata["planning_topic"] == "역전파"
    assert chunks[0].metadata["planning_reason"] == "하나의 개념 설명"
    assert chunks[0].metadata["summary_hint"] == "역전파 정의를 정리합니다."
    assert chunks[0].metadata["segment_count"] == 2
    assert chunks[1].segment_start_index == 2
    assert chunks[1].segment_end_index == 2


def test_context_planned_chunk_builder_preserves_fallback_planning_error() -> None:
    builder = ContextPlannedChunkBuilder()

    chunks = builder.build(
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


def test_deterministic_fallback_chunk_builder_uses_lecture_time_ranges() -> None:
    builder = DeterministicFallbackChunkBuilder(lecture_max_seconds=180.0)

    chunks = builder.build(
        [
            SegmentCreate(
                segment_index=0,
                start_seconds=0.0,
                end_seconds=60.0,
                text="첫 번째 개념입니다.",
            ),
            SegmentCreate(
                segment_index=1,
                start_seconds=70.0,
                end_seconds=120.0,
                text="같은 개념을 계속 설명합니다.",
            ),
            SegmentCreate(
                segment_index=2,
                start_seconds=130.0,
                end_seconds=200.0,
                text="다음 개념입니다.",
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
    assert chunks[0].chunk_strategy == "lecture_context_fallback_v1"
    assert chunks[0].metadata["planning_method"] == "fallback"
    assert "180초" in chunks[0].metadata["planning_reason"]
