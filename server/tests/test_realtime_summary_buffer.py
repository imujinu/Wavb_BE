import pytest

from services.realtime.summary_buffer import RealtimeSummaryBuffer


class FakeSummaryService:
    """summarize_with_keywords만 흉내 내는 테스트용 더블."""

    def __init__(self, summary: str = "요약", keywords: list[str] | None = None) -> None:
        self.summary = summary
        self.keywords = keywords if keywords is not None else ["k1", "k2"]
        self.calls: list[str] = []

    async def summarize_with_keywords(self, text: str) -> tuple[str, list[str]]:
        self.calls.append(text)
        return self.summary, list(self.keywords)


class FailingSummaryService:
    async def summarize_with_keywords(self, text: str) -> tuple[str, list[str]]:
        raise RuntimeError("summary boom")


def make_buffer(**kwargs) -> tuple[RealtimeSummaryBuffer, FakeSummaryService]:
    fake = FakeSummaryService(**kwargs)
    buffer = RealtimeSummaryBuffer(threshold_seconds=25.0, summary_service=fake)
    return buffer, fake


async def test_add_tracks_final_index_range() -> None:
    buffer, _ = make_buffer()
    buffer.add("첫 문장", final_index=3)
    buffer.add("둘째 문장", final_index=4)
    buffer.add("   ", final_index=5)  # 빈 텍스트 → 누적·범위 갱신 안 됨

    full_text, summary, keywords, start_idx, end_idx = await buffer.flush_with_summary()

    assert full_text == "첫 문장 둘째 문장"
    assert summary == "요약"
    assert keywords == ["k1", "k2"]
    assert start_idx == 3
    assert end_idx == 4  # 빈 final(5)은 범위에 포함되지 않음


async def test_flush_resets_buffer_and_range() -> None:
    buffer, _ = make_buffer()
    buffer.add("문장", final_index=0)
    await buffer.flush_with_summary()

    assert buffer.is_empty

    # 다음 구간은 새 인덱스부터 독립적으로 추적되어야 한다.
    buffer.add("다음 구간", final_index=10)
    _, _, _, start_idx, end_idx = await buffer.flush_with_summary()

    assert start_idx == 10
    assert end_idx == 10


async def test_flush_empty_range_returns_negative_one() -> None:
    buffer, _ = make_buffer()

    # add 없이 flush — 누적된 final이 없는 빈 구간
    full_text, _summary, _keywords, start_idx, end_idx = await buffer.flush_with_summary()

    assert full_text == ""
    assert start_idx == -1
    assert end_idx == -1


async def test_drain_resets_buffer_and_snapshot_isolated() -> None:
    buffer, fake = make_buffer()
    buffer.add("first", final_index=0)

    snapshot = buffer.drain()

    assert snapshot.full_text == "first"
    assert snapshot.start_final_index == 0
    assert snapshot.end_final_index == 0
    assert buffer.is_empty

    buffer.add("second", final_index=1)
    summary, keywords = await buffer.summarize_snapshot(snapshot)

    assert summary == fake.summary
    assert keywords == ["k1", "k2"]
    assert fake.calls == ["first"]

    next_snapshot = buffer.drain()

    assert next_snapshot.full_text == "second"
    assert next_snapshot.start_final_index == 1
    assert next_snapshot.end_final_index == 1


async def test_flush_failure_preserves_segments_and_range() -> None:
    buffer = RealtimeSummaryBuffer(
        threshold_seconds=25.0, summary_service=FailingSummaryService()
    )
    buffer.add("보존될 문장", final_index=2)

    with pytest.raises(RuntimeError):
        await buffer.flush_with_summary()

    # 실패 시 세그먼트·범위를 버리지 않아야 다음 flush에서 재포함된다.
    buffer._summary_service = FakeSummaryService()
    full_text, _summary, _keywords, start_idx, end_idx = await buffer.flush_with_summary()

    assert full_text == "보존될 문장"
    assert start_idx == 2
    assert end_idx == 2
