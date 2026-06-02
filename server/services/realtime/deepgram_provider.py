import json
from typing import AsyncGenerator

import websockets

from services.realtime.stt_provider import STTProvider, TranscriptEvent
from settings import get_settings


class DeepgramProvider(STTProvider):
    """
    Deepgram Nova-3 기반 실시간 전사.

    Deepgram을 사용하는 이유:
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
