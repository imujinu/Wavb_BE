import time
from dataclasses import dataclass

from services.summary.summary_service import SummaryService


DEFAULT_REALTIME_SUMMARY_THRESHOLD_SECONDS = 25.0


@dataclass(frozen=True)
class RealtimeSummarySnapshot:
    full_text: str
    start_final_index: int
    end_final_index: int

    @property
    def is_empty(self) -> bool:
        return not self.full_text.strip()


class RealtimeSummaryBuffer:
    """실시간 전사 텍스트를 누적하고 시간 임계값 도달 시 요약을 생성하는 버퍼."""

    def __init__(
        self,
        threshold_seconds: float = DEFAULT_REALTIME_SUMMARY_THRESHOLD_SECONDS,
        summary_service: SummaryService | None = None,
    ) -> None:
        self._segments: list[str] = []
        self._start_time: float = time.monotonic()
        self._threshold = threshold_seconds
        # DI 패턴: 외부 주입이 없으면 기본 인스턴스 생성 (테스트 시 목 주입 가능)
        self._summary_service = summary_service or SummaryService()
        # 이번 구간이 덮는 final 범위 — FE가 정확히 collapse하도록 summary 이벤트에 실어 보낸다.
        # _start_final_index=None은 "아직 누적된 final 없음"을 의미.
        self._start_final_index: int | None = None
        self._end_final_index: int = -1

    def add(self, text: str, final_index: int) -> None:
        """
        기능 요약: is_final=True 텍스트만 버퍼에 누적하고, 덮은 final 범위를 갱신한다.

        왜 is_final만 누적하는가:
            Deepgram interim 결과는 누적이 아니라 대체(replacement)다.
            같은 발화에 대해 "안녕" → "안녕하세요"(final)처럼 오므로,
            interim을 버퍼에 쌓으면 중복 텍스트로 요약이 망가진다.
            호출자(routes/realtime.py)는 is_final=True일 때만 이 메서드를 호출해야 한다.

        왜 final_index를 함께 받는가:
            요약이 어느 final까지 덮었는지를 summary 이벤트에 실어야 FE가
            해당 범위의 실시간 라인만 정확히 접을(collapse) 수 있다.

        기능 흐름:
            1. 공백 제거 후 빈 문자열이면 무시 (노이즈 방지 — 범위도 갱신하지 않음)
            2. 텍스트를 세그먼트 목록에 추가
            3. final 범위 갱신 (첫 누적이면 start 설정, end는 항상 최신값)

        파라미터:
            text: Deepgram is_final=True transcript 텍스트
            final_index: 이 final의 단조 증가 인덱스 (routes에서 부여)
        """
        # 1. 공백 제거 후 빈 문자열이면 무시 (공백·빈 전사 결과가 버퍼에 쌓이는 것 방지)
        text = text.strip()
        if not text:
            return
        # 2. 텍스트 누적
        self._segments.append(text)
        # 3. 범위 갱신 — 첫 누적 final을 start로, 매 final을 end로
        if self._start_final_index is None:
            self._start_final_index = final_index
        self._end_final_index = final_index

    def should_flush(self) -> bool:
        """
        기능 요약: is_final 시점에 호출 — 시간 임계값 초과 여부를 반환한다.

        왜 is_final 시점에만 호출하는가:
            임계값 체크는 확정된 결과가 왔을 때만 의미있다.
            interim 도중 flush되면 미완성 문장이 요약에 포함될 수 있다.
        """
        return (time.monotonic() - self._start_time) >= self._threshold

    def drain(self) -> RealtimeSummarySnapshot:
        """
        Capture the current buffered final transcripts and immediately reset the buffer.

        This keeps summary generation from racing with the live transcript stream: the
        background summary task works on the returned snapshot while new final
        transcripts start filling a fresh buffer.
        """
        snapshot = RealtimeSummarySnapshot(
            full_text=" ".join(self._segments),
            start_final_index=(
                self._start_final_index if self._start_final_index is not None else -1
            ),
            end_final_index=self._end_final_index,
        )
        self._segments = []
        self._start_final_index = None
        self._end_final_index = -1
        self._start_time = time.monotonic()
        return snapshot

    async def summarize_snapshot(
        self,
        snapshot: RealtimeSummarySnapshot,
    ) -> tuple[str, list[str]]:
        return await self._summary_service.summarize_with_keywords(snapshot.full_text)

    async def flush_with_summary(self) -> tuple[str, str, list[str], int, int]:
        """
        기능 요약: 누적 텍스트를 요약·키워드 추출하고 버퍼를 초기화한다.

        기능 흐름:
            1. 누적 세그먼트를 공백으로 합쳐 full_text 생성
            2. SummaryService.summarize_with_keywords()로 요약+키워드 생성
               - 실패 시: 타이머만 리셋하고 세그먼트·범위 보존 후 예외 재발생
                 (세그먼트를 버리지 않아야 내용 손실 없이 다음 flush 시 재포함 가능)
            3. 반환할 final 범위 스냅샷 확보 (리셋 전)
            4. 버퍼·범위·타이머 초기화

        반환:
            (full_text, summary, keywords, start_final_index, end_final_index) 5-튜플
        """
        # 1. 누적 세그먼트를 공백으로 합쳐 full_text 생성
        full_text = " ".join(self._segments)
        try:
            # 2. 요약 + 키워드 단일 호출
            summary, keywords = await self._summary_service.summarize_with_keywords(full_text)
        except Exception:
            # 요약 실패 시 타이머만 리셋하여 즉시 재시도 루프를 방지
            # 세그먼트·범위는 보존해 다음 flush 때 내용이 포함되도록 한다
            self._start_time = time.monotonic()
            raise
        # 3. 리셋 전에 범위 스냅샷 확보 (start가 None이면 빈 구간 → -1로 표기)
        start_index = self._start_final_index if self._start_final_index is not None else -1
        end_index = self._end_final_index
        # 4. 요약 성공 시 버퍼·범위·타이머 초기화
        self._segments = []
        self._start_final_index = None
        self._end_final_index = -1
        self._start_time = time.monotonic()
        return full_text, summary, keywords, start_index, end_index

    @property
    def is_empty(self) -> bool:
        """버퍼에 누적된 세그먼트가 없는지 여부를 반환한다."""
        return len(self._segments) == 0
