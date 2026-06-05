# 실시간 전사 WebSocket 재구현 플랜

**Goal:** 기존 realtime 코드를 전면 삭제하고, STTProvider 추상화 레이어 위에 Deepgram Nova-3 기반 실시간 전사 WebSocket을 재구현한다.

**Architecture:** 모바일이 PCM16(16kHz mono)을 WebSocket으로 스트리밍 → 서버가 `STTProvider` 인터페이스를 통해 Deepgram으로 포워딩 → 전사 결과를 JSON으로 클라이언트에 역전달. `STTProvider` 추상화는 유지하여 추후 모델 교체에 대비한다.

**Tech Stack:** FastAPI WebSocket, websockets 라이브러리, Deepgram Nova-3 Streaming API, asyncio.gather (동시 I/O)

**클라이언트 오디오 프로토콜:**

| 항목 | 값 |
|------|-----|
| 포맷 | Raw PCM16 (signed 16-bit little-endian) |
| 샘플레이트 | 16000Hz |
| 채널 | 1 (mono) |
| 전송 방식 | WebSocket binary, 연속 스트리밍 |
| 연결 URL | `ws://host/audio/realtime/connect?token=<JWT>` |

---

## Phase 1: 기존 파일 삭제

왜 삭제하는가: 기존 설계가 구조적으로 잘못되어 부분 수정보다 재구현이 명확합니다. 기존 파일을 남기면 새 코드와 혼용됩니다.

- [ ] 구 파일 삭제

```bash
cd server
git rm routes/realtime.py
git rm services/realtime_transcription_service.py
git rm schemas/realtime.py
```

- [ ] main.py에서 realtime router import 임시 제거

`server/main.py`에서 아래 두 줄 삭제 (Phase 3에서 다시 추가):

```python
# 삭제
from routes.realtime import router as realtime_router
app.include_router(realtime_router)
```

- [ ] 서버 기동 확인

```bash
uv run uvicorn main:app --reload
# 예상: 정상 기동 (realtime 없이)
```

---

## Phase 2: STT Provider 추상화 레이어

왜 추상화를 유지하는가: 현재는 Deepgram을 사용하지만, 추후 다른 STT 모델로 교체할 가능성이 있습니다. `STTProvider` 인터페이스를 두면 route 코드 변경 없이 provider만 교체할 수 있습니다.

### Task 1: services/realtime/ 패키지 생성

- [ ] 디렉터리 + `__init__.py` 생성

```bash
mkdir server/services/realtime
"" | Out-File server/services/realtime/__init__.py -Encoding utf8
```

### Task 2: STTProvider 추상 기반 클래스

**File:** `server/services/realtime/stt_provider.py`

- [ ] 추상 인터페이스 작성

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncGenerator


# 왜 dataclass인가: provider ↔ route 간 경량 데이터 교환.
# Pydantic은 DB/HTTP 경계에서만 사용.
@dataclass
class TranscriptEvent:
    type: str           # "transcript" | "error"
    text: str = ""      # 전사된 텍스트 (type="transcript"일 때)
    is_final: bool = False  # True: 최종 결과, False: 중간 결과(interim)
    message: str = ""   # 에러 메시지 (type="error"일 때)


class STTProvider(ABC):
    """
    STT provider 추상 인터페이스.

    생명주기: connect() → [send_audio() 반복] → disconnect()
    transcript_events()는 connect() 직후, send_audio()와 동시에 소비해야 합니다.
    """

    @abstractmethod
    async def connect(self) -> None:
        """STT 서비스에 연결하고 초기 설정을 전송합니다."""
        ...

    @abstractmethod
    async def send_audio(self, pcm16_bytes: bytes) -> None:
        """
        PCM16 16000Hz mono 오디오를 전달합니다.
        provider 내부에서 필요한 포맷 변환을 처리합니다.
        """
        ...

    @abstractmethod
    async def transcript_events(self) -> AsyncGenerator[TranscriptEvent, None]:
        """
        전사 이벤트를 비동기 스트리밍합니다.
        disconnect()가 호출될 때까지 계속 실행됩니다.

        왜 AsyncGenerator인가: 응답이 오는 즉시 클라이언트에 전달해야 합니다.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """연결을 정상 종료합니다."""
        ...
```



### Task 3: Deepgram Nova-3 Provider

**File:** `server/services/realtime/deepgram_provider.py`

- [ ] Deepgram provider 구현

```python
import json
from typing import AsyncGenerator

import websockets

from services.realtime.stt_provider import STTProvider, TranscriptEvent
from settings import get_settings


class DeepgramProvider(STTProvider):
    """
    Deepgram Nova-3 기반 실시간 전사.

    왜 Deepgram인가:
    - Nova-3는 한국어를 지원하며 스트리밍 레이턴시가 낮습니다.
    - PCM16 16kHz를 직접 지원해 서버 측 리샘플링이 불필요합니다.
    - interim_results=true로 중간 결과도 즉시 수신합니다.

    프로토콜:
    1. WSS 연결 (Token 인증), 쿼리 파라미터로 오디오 포맷 지정
    2. 바이너리 PCM16 raw bytes 직접 전송 (base64 인코딩 불필요)
    3. JSON 형식의 interim/final 전사 결과 수신
    """

    _WS_URL = (
        "wss://api.deepgram.com/v1/listen"
        "?model=nova-3"
        "&language=ko"
        "&encoding=linear16"
        "&sample_rate=16000"
        "&channels=1"
        "&punctuate=true"
        "&interim_results=true"
    )

    def __init__(self) -> None:
        self._ws = None
        self._settings = get_settings()

    async def connect(self) -> None:
        headers = {"Authorization": f"Token {self._settings.deepgram_api_key}"}
        self._ws = await websockets.connect(
            self._WS_URL, additional_headers=headers
        )

    async def send_audio(self, pcm16_bytes: bytes) -> None:
        # 왜 바이너리 직접 전송인가:
        # Deepgram streaming API는 raw binary PCM을 그대로 받습니다.
        # base64 인코딩 없이 전송해 CPU 오버헤드를 줄입니다.
        await self._ws.send(pcm16_bytes)

    async def transcript_events(self) -> AsyncGenerator[TranscriptEvent, None]:
        async for raw_msg in self._ws:
            if isinstance(raw_msg, bytes):
                # Deepgram이 간혹 binary keepalive를 전송합니다.
                continue

            msg = json.loads(raw_msg)
            if msg.get("type") != "Results":
                continue

            alternatives = msg.get("channel", {}).get("alternatives", [])
            if not alternatives:
                continue

            transcript = alternatives[0].get("transcript", "")
            is_final = msg.get("is_final", False)

            if transcript:
                yield TranscriptEvent(
                    type="transcript",
                    text=transcript,
                    is_final=is_final,
                )

    async def disconnect(self) -> None:
        if self._ws:
            # Deepgram 종료 신호 — 이 메시지를 받으면 서버가 연결을 닫습니다.
            await self._ws.send(json.dumps({"type": "CloseStream"}))
            await self._ws.close()
            self._ws = None
```

- [ ] 커밋

```bash
git add server/services/realtime/deepgram_provider.py
git commit -m "feat: Deepgram Nova-3 STT provider 구현"
```

### Task 4: Provider Factory + Settings 추가

**Files:**
- `server/services/realtime/provider_factory.py`
- `server/settings.py` (수정)

- [ ] settings.py에 Deepgram 설정 추가

`server/settings.py`의 Settings 클래스에 추가:

```python
# Deepgram API 키 (실시간 전사용)
deepgram_api_key: str = ""

# STT provider 선택 — 추후 모델 교체 시 변경
# 지원 값: "deepgram" (현재)
stt_provider: str = "deepgram"
```

- [ ] provider_factory.py 작성

```python
from services.realtime.stt_provider import STTProvider
from services.realtime.deepgram_provider import DeepgramProvider
from settings import get_settings


def create_stt_provider() -> STTProvider:
    """
    STT_PROVIDER env 변수에 따라 provider 인스턴스를 생성합니다.

    왜 factory 함수인가:
    - WebSocket 세션마다 새 인스턴스가 필요합니다 (세션 상태 격리).
    - provider 타입을 런타임에 결정하므로 재배포 없이 전환 가능합니다.

    현재 지원: "deepgram" (기본값)
    추후 추가 시: settings.stt_provider 분기 확장
    """
    # 현재는 Deepgram만 지원. 추후 다른 provider 추가 시 분기 확장.
    return DeepgramProvider()
```

- [ ] .env에 DEEPGRAM_API_KEY 추가

`server/.env`:
```
DEEPGRAM_API_KEY=your_deepgram_api_key_here
```



---

## Phase 3: Schemas + Route 재구현

### Task 5: Schemas 재구현

**File:** `server/schemas/realtime.py`

- [ ] schemas 작성

```python
from pydantic import BaseModel


class RealtimeSegmentInput(BaseModel):
    """저장 요청 시 클라이언트가 보내는 개별 세그먼트."""
    segment_index: int
    start_seconds: float
    end_seconds: float
    text: str


class RealtimeSaveRequest(BaseModel):
    """
    녹음 종료 후 전체 세그먼트를 DB에 저장하는 요청.

    왜 WebSocket이 아닌 HTTP POST인가:
    - WebSocket 세션 중에는 클라이언트가 임시 전사 결과를 로컬에 누적합니다.
    - 녹음 완료 후 한 번에 저장해 부분 저장/롤백 복잡도를 없앱니다.
    """
    domain_type: str       # "general", "legal", "medical", "science", "it", "religion"
    title: str
    duration_seconds: float
    segments: list[RealtimeSegmentInput]


class RealtimeSaveResponse(BaseModel):
    transcript_id: str
    segment_count: int
```


### Task 6: WebSocket Route 재구현

**File:** `server/routes/realtime.py`

- [ ] route 작성

```python
import asyncio
from dataclasses import asdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from schemas.realtime import RealtimeSaveRequest, RealtimeSaveResponse
from services.audio.transcript_ingestion_service import TranscriptIngestionService
from services.realtime.provider_factory import create_stt_provider

router = APIRouter(prefix="/audio", tags=["realtime"])


@router.websocket("/realtime/connect")
async def realtime_connect(websocket: WebSocket, token: str) -> None:
    """
    실시간 전사 WebSocket 엔드포인트.

    클라이언트 프로토콜:
    - 연결: ws://host/audio/realtime/connect?token=<JWT>
    - 전송: binary PCM16 16kHz mono 연속 스트리밍
    - 수신: {"type": "ready"} 연결 확인
            {"type": "transcript", "text": "...", "is_final": true|false}
            {"type": "error", "message": "..."}
    - 종료: 클라이언트가 WebSocket close

    왜 asyncio.gather로 두 태스크를 동시 실행하는가:
    오디오 수신(forward_audio)과 전사 결과 전달(forward_transcripts)은
    독립적인 I/O 작업입니다. 순차 실행 시 한쪽이 블록되면 다른 쪽도 멈춥니다.
    """
    # JWT 검증 — 유효하지 않으면 4001 코드로 거절
    # 왜 4001인가: 표준 WebSocket close code 중 인증 실패를 나타내는 관례적 값
    try:
        await _validate_ws_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    await websocket.accept()

    provider = create_stt_provider()
    await provider.connect()
    await websocket.send_json({"type": "ready"})

    async def forward_audio() -> None:
        """모바일 → Deepgram: 바이너리 청크 수신 후 provider로 전달."""
        try:
            async for chunk in websocket.iter_bytes():
                await provider.send_audio(chunk)
        except WebSocketDisconnect:
            pass
        finally:
            # 클라이언트 연결 종료 시 provider도 정상 종료
            await provider.disconnect()

    async def forward_transcripts() -> None:
        """Deepgram → 모바일: 전사 이벤트를 JSON으로 전달."""
        try:
            async for event in provider.transcript_events():
                await websocket.send_json(asdict(event))
        except WebSocketDisconnect:
            pass

    await asyncio.gather(forward_audio(), forward_transcripts())


async def _validate_ws_token(token: str) -> dict:
    """
    WebSocket query parameter로 전달된 JWT를 검증합니다.

    왜 query parameter인가:
    WebSocket 핸드셰이크는 커스텀 HTTP 헤더를 브라우저/앱에서 설정하기
    어려우므로 JWT를 query parameter로 전달하는 것이 일반적입니다.

    구현: 기존 routes/auth.py의 JWT 검증 로직을 참고해 구현합니다.
    settings.secret_key와 jose 라이브러리를 사용합니다.
    """
    from jose import jwt, JWTError
    from settings import get_settings

    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return payload
    except JWTError:
        raise ValueError("유효하지 않은 토큰")


@router.post("/transcripts/realtime", response_model=RealtimeSaveResponse)
async def save_realtime_transcript(body: RealtimeSaveRequest) -> RealtimeSaveResponse:
    """
    실시간 녹음 세션 종료 후 전체 전사 결과를 DB에 저장합니다.
    TranscriptIngestionService를 통해 세그먼트 → 청크 → 임베딩 파이프라인을 실행합니다.
    """
    ingestion_service = TranscriptIngestionService()
    result = await ingestion_service.ingest_realtime_segments(
        domain_type=body.domain_type,
        title=body.title,
        duration_seconds=body.duration_seconds,
        segments=[s.model_dump() for s in body.segments],
    )
    return RealtimeSaveResponse(
        transcript_id=str(result.transcript_id),
        segment_count=result.segment_count,
    )
```

- [ ] main.py에 realtime router 재등록

`server/main.py`:
```python
from routes.realtime import router as realtime_router
app.include_router(realtime_router)
```



### Task 7: TranscriptIngestionService에 ingest_realtime_segments 추가

**File:** `server/services/audio/transcript_ingestion_service.py`

`ingest_realtime_segments` 메서드가 없다면 추가합니다.
이 메서드는 STT 없이 이미 전사된 세그먼트를 받아 DB 저장 + 청크 빌딩만 수행합니다.
기존 `ingest()` 메서드의 7번째 단계(RagRepository → persist segments) 이후 로직을 참고합니다.

```python
async def ingest_realtime_segments(
    self,
    domain_type: str,
    title: str,
    duration_seconds: float,
    segments: list[dict],
) -> TranscriptIngestionResult:
    """
    실시간 전사 세그먼트를 DB에 저장하고 검색 청크를 생성합니다.

    왜 별도 메서드인가:
    - 실시간 전사에서는 STT 호출이 이미 Deepgram에서 완료됨
    - STT 단계를 건너뛰고 저장 + 청크 빌딩 단계부터 시작
    """
    # 기존 ingest() 7단계 이후 로직을 여기에 구현
    # transcript 레코드 생성 → segments 저장 → chunks 빌딩 → 임베딩 → 저장
    ...
```



---

## Phase 4: 최종 검증

- [ ] 서버 기동 확인

```bash
cd server
uv run uvicorn main:app --reload
# http://localhost:8000/docs 에서 realtime 엔드포인트 확인
```

- [ ] 전체 import 체인 검증

```bash
uv run python -c "
from services.realtime.provider_factory import create_stt_provider
from services.realtime.deepgram_provider import DeepgramProvider
from routes.realtime import router
provider = create_stt_provider()
print(type(provider).__name__)  # 예상: DeepgramProvider
print('realtime import 성공')
"
```

- [ ] 기존 테스트 전체 실행

```bash
uv run pytest tests/ --tb=short
```

---

## 파일 변경 요약

| 작업 | 파일 |
|------|------|
| 삭제 | `services/realtime_transcription_service.py`, `routes/realtime.py` (구), `schemas/realtime.py` (구) |
| 신규 생성 | `services/realtime/stt_provider.py`, `deepgram_provider.py`, `provider_factory.py` |
| 재생성 | `routes/realtime.py`, `schemas/realtime.py` |
| 수정 | `settings.py`, `main.py`, `services/audio/transcript_ingestion_service.py` |
