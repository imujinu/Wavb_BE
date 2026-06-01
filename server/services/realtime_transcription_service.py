import logging
import os
import tempfile

from fastapi import HTTPException, status
from openai import APIError, AsyncOpenAI

from schemas.realtime import RealtimeTranscriptEvent
from settings import get_settings

logger = logging.getLogger(__name__)


class RealtimeTranscriptionService:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OPENAI_API_KEY is not configured.",
            )
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_stt_model

    async def transcribe_chunk(
        self,
        audio_bytes: bytes,
        chunk_index: int,
        accumulated_text: str = "",
    ) -> RealtimeTranscriptEvent:
        if not audio_bytes:
            return RealtimeTranscriptEvent(
                type="error",
                chunk_index=chunk_index,
                message="빈 오디오 데이터입니다.",
            )

        tmp_path: str | None = None
        try:
            # 1. bytes → 임시 파일 저장
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            # 2. Whisper-1 호출 (이전 전사 텍스트를 prompt로 전달해 문맥 연속성 확보)
            prompt = accumulated_text[-200:].strip() if accumulated_text else None
            with open(tmp_path, "rb") as audio_file:
                response = await self._client.audio.transcriptions.create(
                    model=self._model,
                    file=audio_file,
                    language="ko",
                    response_format="text",
                    prompt=prompt,
                )

            text = str(response).strip() if response else ""

            # 3. 성공 이벤트 반환
            return RealtimeTranscriptEvent(
                type="transcript",
                chunk_index=chunk_index,
                text=text,
            )

        except APIError as exc:
            logger.warning("Whisper API 오류 (chunk %d): %s", chunk_index, exc)
            return RealtimeTranscriptEvent(
                type="error",
                chunk_index=chunk_index,
                message=f"전사 API 오류: {exc}",
            )
        except Exception as exc:
            logger.warning("청크 전사 실패 (chunk %d): %s", chunk_index, exc)
            return RealtimeTranscriptEvent(
                type="error",
                chunk_index=chunk_index,
                message="청크 전사 중 오류가 발생했습니다.",
            )
        finally:
            # 4. 임시 파일 삭제
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
