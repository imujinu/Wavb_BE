from schemas.rag import SegmentCreate
from services.chunk_builder import LectureChunkBuilder, MeetingChunkBuilder, get_chunk_builder


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
    assert chunks[1].segment_start_index == 1
    assert chunks[1].segment_end_index == 2
    assert chunks[1].metadata["overlap_from_previous"] == 1


def test_get_chunk_builder_selects_domain_strategy() -> None:
    assert isinstance(get_chunk_builder("meeting"), MeetingChunkBuilder)
    assert isinstance(get_chunk_builder("lecture"), LectureChunkBuilder)
