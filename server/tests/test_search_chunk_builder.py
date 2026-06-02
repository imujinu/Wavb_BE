from uuid import uuid4

from schemas.rag import ChunkRow, SearchChunkCreate, SegmentCreate
from services.chunks.search_chunk_builder import SearchChunkBuilder


# 테스트용 SegmentCreate를 간결하게 생성하는 헬퍼.
# start/end 미제공 시 index 기반 기본 시간값(index*10, index*10+8)을 사용한다.
def make_segment(
    index: int,
    text: str,
    start: float | None = None,
    end: float | None = None,
) -> SegmentCreate:
    return SegmentCreate(
        segment_index=index,
        start_seconds=start if start is not None else float(index * 10),
        end_seconds=end if end is not None else float(index * 10 + 8),
        text=text,
    )


# 테스트용 ChunkRow를 간결하게 생성하는 헬퍼.
# start_seconds, end_seconds, text, metadata는 테스트에서 검증하지 않으므로 더미값으로 채운다.
def make_chunk_row(chunk_index: int, seg_start: int, seg_end: int) -> ChunkRow:
    return ChunkRow(
        id=uuid4(),
        chunk_index=chunk_index,
        segment_start_index=seg_start,
        segment_end_index=seg_end,
        start_seconds=None,
        end_seconds=None,
        text="dummy",
        metadata={},
    )


def test_adaptive_grouping_stays_within_parent_range() -> None:
    # parent chunk가 segments 0~8을 포함할 때
    # 모든 child의 segment 범위가 parent 범위를 벗어나지 않는지 확인한다.
    chunk = make_chunk_row(chunk_index=0, seg_start=0, seg_end=8)
    segments = [make_segment(i, "x" * 50) for i in range(9)]

    builder = SearchChunkBuilder()
    children: list[SearchChunkCreate] = builder.build([chunk], segments)

    assert len(children) > 0
    for child in children:
        assert child.segment_start_index >= 0
        assert child.segment_end_index <= 8
        assert child.metadata["child_goal"] == "vector_search"
        assert child.metadata["grouping_strategy"] == "adaptive_segments_v1"


def test_short_segments_grouped_into_2_to_3() -> None:
    # 50글자 짧은 segments 6개는 max_chars=800, max_seconds=90 기준으로 묶여
    # child 수가 6개 미만이어야 한다 (단독으로 저장되지 않고 그루핑됨).
    # 마지막 child는 홀수 분배로 segment 1개가 될 수 있으므로 제외하고 확인한다.
    chunk = make_chunk_row(chunk_index=0, seg_start=0, seg_end=5)
    segments = [make_segment(i, "x" * 50) for i in range(6)]

    builder = SearchChunkBuilder(max_chars=800, max_seconds=90)
    children: list[SearchChunkCreate] = builder.build([chunk], segments)

    # 묶임 확인: 6개 segment가 6개 child로 분리되면 안 됨
    assert len(children) < 6

    # 마지막 child를 제외한 나머지 child는 segment 2개 이상으로 구성되어야 한다
    for child in children[:-1]:
        assert child.metadata["segment_count"] >= 2


def test_long_segment_becomes_standalone_child() -> None:
    # 900글자 segment(max_chars=800 초과)는 단독 child로 저장되어야 한다.
    # 앞뒤 short segment와 묶이지 않고 분리되는지 확인한다.
    chunk = make_chunk_row(chunk_index=0, seg_start=0, seg_end=2)
    segments = [
        make_segment(0, "short text"),
        make_segment(1, "a" * 900),
        make_segment(2, "short text"),
    ]

    builder = SearchChunkBuilder(max_chars=800)
    children: list[SearchChunkCreate] = builder.build([chunk], segments)

    # segment 1(900글자)이 단독 child인지 확인
    standalone = [
        c for c in children
        if c.segment_start_index == 1 and c.segment_end_index == 1
    ]
    assert len(standalone) == 1


def test_max_chars_and_seconds_not_exceeded() -> None:
    # max_chars=800, max_seconds=90 기준으로 빌드했을 때
    # 모든 child의 텍스트 길이가 800자 이하이고
    # 시간 범위가 90초 이하인지 확인한다.
    chunk = make_chunk_row(chunk_index=0, seg_start=0, seg_end=5)
    segments = [
        # 각 segment는 200글자, 시간 간격이 넓어 빠르게 max_seconds에 도달
        make_segment(i, "y" * 200, start=float(i * 40), end=float(i * 40 + 30))
        for i in range(6)
    ]

    builder = SearchChunkBuilder(max_chars=800, max_seconds=90)
    children: list[SearchChunkCreate] = builder.build([chunk], segments)

    assert len(children) > 0
    for child in children:
        assert len(child.text) <= 800
        if child.start_seconds is not None and child.end_seconds is not None:
            assert child.end_seconds - child.start_seconds <= 90
