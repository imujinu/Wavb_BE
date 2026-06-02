from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncGenerator


# provider ↔ route 간 경량 데이터 교환.
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

        AsyncGenerator를 사용하는 이유: 응답이 오는 즉시 클라이언트에 전달해야 합니다.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """연결을 정상 종료합니다."""
        ...
