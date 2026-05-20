import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from openai import APIError, AsyncOpenAI

from settings import get_settings

#todo : Exception handling 공통화하기

class TranscriptionService:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OPENAI_API_KEY is not configured.",
            )
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_stt_model

    async def transcribe(self, file: UploadFile) -> str:
        suffix = Path(file.filename or "").suffix

        try:
            with tempfile.NamedTemporaryFile(delete=True, suffix=suffix) as temp_file:
                while chunk := await file.read(1024 * 1024):
                    temp_file.write(chunk)

                if temp_file.tell() == 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Uploaded audio file is empty.",
                    )

                temp_file.flush()
                temp_file.seek(0)

                transcription = await self._client.audio.transcriptions.create(
                    model=self._model,
                    file=temp_file.file,
                    language="ko",
                    response_format="text",
                )
        except HTTPException:
            raise
        except APIError as exc:
            print(exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Audio transcription provider failed.",
            ) from exc
        except Exception as exc:
            print(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Audio transcription failed.",
            ) from exc

        return str(transcription).strip()
