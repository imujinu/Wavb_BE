# 실시간 전사 구간 자동 요약 플랜

**목표:** Deepgram 실시간 전사 스트림에서 20-30초 분량의 `is_final` 텍스트가 쌓이면 GPT 요약을 생성하여 `{"type": "summary"}` WebSocket 메시지로 전달한다.

**프론트엔드 처리:** summary 수신 시 해당 구간을 요약 텍스트로 대체, 원문은 버튼으로 노출, 구간별 타임스탬프 표시.

---

## 전체 흐름

```
클라이언트 오디오 전송
  → DeepgramProvider.transcript_events() → TranscriptEvent
  → buffer.add(event.text)          # interim/final 모두 누적
  → is_final=True 도달 시 buffer.should_flush() 체크
  → 25초 경과했으면 asyncio.create_task(_send_summary())
    → SummaryService.summarize(full_text)
    → websocket.send_json({"type": "summary", ...})
```

---

## Step 1. `schemas/realtime.py` — `RealtimeSummaryEvent` 추가

**왜:**
WebSocket으로 내보내는 메시지 타입을 Pydantic 모델로 정의해야 직렬화/검증이 일관된다.
`type: Literal["summary"]`로 고정해 프론트가 타입 분기 시 오타 없이 처리할 수 있다.

**추가할 모델:**
```python
from typing import Literal

class RealtimeSummaryEvent(BaseModel):
    type: Literal["summary"] = "summary"
    summary: str        # GPT 요약문
    full_text: str      # 원문 전체 (프론트 "전체 보기" 버튼용)
    segment_index: int  # 몇 번째 구간인지 (0부터, 프론트 렌더링 매칭용)
```

**파일:** `server/schemas/realtime.py`

---

## Step 2. `services/realtime/summary_buffer.py` — 신규 생성

**왜:**
누적 버퍼 상태(텍스트 목록, 타이머)와 요약 트리거 조건을 route에서 분리한다.
route는 "언제 요약을 트리거할지"를 몰라도 되고, buffer 교체/테스트가 독립적으로 가능하다.

**왜 wall-clock 타이머(time.monotonic)인가:**
Deepgram의 `TranscriptEvent`에는 현재 타임스탬프가 없다. STTProvider 인터페이스를 변경하지 않고도 경과 시간을 측정할 수 있는 가장 단순한 방법이다.

**왜 threshold_seconds=25.0인가:**
20-30초 범위의 중간값. 너무 짧으면 요약 빈도가 높아 GPT 비용 증가, 너무 길면 한 번에 처리할 텍스트가 많아져 요약 품질 저하.

```python
import time
from services.summary.summary_service import SummaryService

class RealtimeSummaryBuffer:
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
        full_text = " ".join(self._segments)
        summary = await self._summary_service.summarize(full_text)
        self._segments = []
        self._start_time = time.monotonic()
        return full_text, summary

    @property
    def is_empty(self) -> bool:
        return len(self._segments) == 0
```

**파일:** `server/services/realtime/summary_buffer.py`

---

## Step 3. `routes/realtime.py` — `forward_transcripts()` 수정

**왜 `asyncio.create_task()`로 분리하는가:**
GPT 요약 생성은 1-2초 소요된다. `forward_transcripts()`를 블로킹하면 그 동안 Deepgram에서 오는 transcript 이벤트를 처리하지 못해 스트림이 지연된다. `create_task()`로 분리하면 요약 생성 중에도 transcript 전달이 계속된다.

**왜 `_send_summary()`를 별도 함수로 분리하는가:**
`create_task()` 콜백에서 예외가 발생해도 `forward_transcripts()`가 중단되지 않도록 예외 처리를 분리한다. 요약 실패는 에러 메시지로 graceful하게 처리한다.

**수정 내용:**

```python
# 임포트 추가
import asyncio
from schemas.realtime import RealtimeSummaryEvent
from services.realtime.summary_buffer import RealtimeSummaryBuffer

async def forward_transcripts() -> None:
    buffer = RealtimeSummaryBuffer(threshold_seconds=25.0)
    segment_index = 0

    try:
        async for event in provider.transcript_events():
            # 1. 전사 이벤트를 프론트에 즉시 전달
            await websocket.send_json(asdict(event))

            # 2. 텍스트를 버퍼에 누적 (interim/final 모두)
            if event.text:
                buffer.add(event.text)

            # 3. is_final 시점에만 임계값 체크
            # 왜 is_final 시점인가: 확정된 결과가 도달했을 때 구간 완료를 판단한다.
            # interim 도중 flush하면 미완성 문장이 요약에 포함될 수 있다.
            if event.is_final and buffer.should_flush():
                asyncio.create_task(
                    _send_summary(websocket, buffer, segment_index)
                )
                segment_index += 1
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.send_json({
            "type": "error",
            "message": "전사 서비스에 연결할 수 없습니다.",
        })


async def _send_summary(
    websocket: WebSocket,
    buffer: RealtimeSummaryBuffer,
    segment_index: int,
) -> None:
    """
    기능 요약: 버퍼를 flush하고 summary 이벤트를 WebSocket으로 전송한다.
    — forward_transcripts()를 블로킹하지 않도록 별도 태스크로 실행된다.

    기능 흐름:
        1. buffer.flush_with_summary()로 GPT 요약 생성 및 버퍼 초기화
        2. RealtimeSummaryEvent 생성 후 전송
        3. 실패 시 에러 메시지 전송 (스트림 중단 없음)
    """
    try:
        full_text, summary = await buffer.flush_with_summary()
        event = RealtimeSummaryEvent(
            summary=summary,
            full_text=full_text,
            segment_index=segment_index,
        )
        await websocket.send_json(event.model_dump())
    except Exception:
        await websocket.send_json({
            "type": "error",
            "message": "요약 생성에 실패했습니다.",
        })
```

**파일:** `server/routes/realtime.py`

---

## 재사용되는 기존 코드

| 파일 | 재사용 대상 |
|------|------------|
| `services/summary/summary_service.py` | `SummaryService.summarize(text: str) -> str` |
| `services/realtime/stt_provider.py` | `TranscriptEvent.is_final`, `TranscriptEvent.text` |

---

## 변경 파일 요약

| 파일 | 작업 |
|------|------|
| `schemas/realtime.py` | `RealtimeSummaryEvent` 추가 |
| `services/realtime/summary_buffer.py` | 신규 생성 |
| `routes/realtime.py` | `forward_transcripts()` + `_send_summary()` 수정/추가 |

---

## 검증

```bash
cd server

# 유닛 테스트 (buffer 로직)
uv run pytest tests/test_realtime_summary_buffer.py -v

# 서버 기동
uv run uvicorn main:app --reload

# 수동 검증: WebSocket 연결 후 25초 이상 오디오 스트리밍
# → transcript 이벤트 정상 수신
# → 25초 후 summary 이벤트 수신 확인
# → summary 이벤트에 summary, full_text, segment_index 포함 확인
```

---

## 미구현 (추후)

- **재연결 로직:** Deepgram 연결 오류 시 exponential backoff 재시도
- **타이밍 정보:** Deepgram 응답의 실제 audio timestamp를 `TranscriptEvent`에 추가해 정확한 구간 시작/종료 초 전달
- **요약 임계값 설정:** `settings.py`에 `REALTIME_SUMMARY_THRESHOLD_SECONDS` 추가해 환경변수로 제어
