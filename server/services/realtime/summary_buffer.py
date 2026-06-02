import time

from services.summary.summary_service import SummaryService


class RealtimeSummaryBuffer:
    """실시간 전사 텍스트를 누적하고 시간 임계값 도달 시 요약을 생성하는 버퍼."""

    def __init__(self, threshold_seconds: float = 25.0) -> None:
        self._segments: list[str] = []
        self._start_time: float = time.monotonic()
        self._threshold = threshold_seconds
        self._summary_service = SummaryService()

    def add(self, text: str) -> None:
        """
        기능 요약: 전사 텍스트를 버퍼에 누적한다. interim/final 구분 없이 호출한다.

        기능 흐름:
            1. 텍스트를 세그먼트 목록에 추가

        파라미터:
            text: Deepgram transcript 텍스트 (interim 또는 final)
        """
        self._segments.append(text)

    def should_flush(self) -> bool:
        """
        기능 요약: is_final 시점에 호출 — 시간 임계값 초과 여부를 반환한다.

        왜 is_final 시점에만 호출하는가:
            임계값 체크는 확정된 결과가 왔을 때만 의미있다.
            interim 도중 flush되면 미완성 문장이 요약에 포함될 수 있다.
        """
        return (time.monotonic() - self._start_time) >= self._threshold

    async def flush_with_summary(self) -> tuple[str, str]:
        """
        기능 요약: 누적 텍스트를 요약하고 버퍼를 초기화한다.

        기능 흐름:
            1. 누적 세그먼트를 공백으로 합쳐 full_text 생성
            2. SummaryService.summarize()로 GPT 요약 생성
            3. 버퍼 초기화 및 타이머 리셋

        반환:
            (full_text, summary) 튜플
        """
        # 1. 누적 세그먼트를 공백으로 합쳐 full_text 생성
        full_text = " ".join(self._segments)
        # 2. SummaryService.summarize()로 GPT 요약 생성
        summary = await self._summary_service.summarize(full_text)
        # 3. 버퍼 초기화 및 타이머 리셋
        self._segments = []
        self._start_time = time.monotonic()
        return full_text, summary

    @property
    def is_empty(self) -> bool:
        """버퍼에 누적된 세그먼트가 없는지 여부를 반환한다."""
        return len(self._segments) == 0
