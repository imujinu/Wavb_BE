import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from openai import APIError, AsyncOpenAI

from services.audio_analysis_service import AudioAnalysisService
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
        self._audio_analysis_service = AudioAnalysisService()

    async def transcribe(self, file: UploadFile) -> str:
        suffix = Path(file.filename or "").suffix or ".audio"

        try:
            with tempfile.TemporaryDirectory() as temp_dir_name:
                input_path = Path(temp_dir_name) / f"input{suffix}"
                await self._save_upload(file, input_path)
                self._audio_analysis_service.analyze(input_path)

                with input_path.open("rb") as audio_file:
                    transcription = await self._client.audio.transcriptions.create(
                        model=self._model,
                        file=audio_file,
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

    async def _save_upload(self, file: UploadFile, input_path: Path) -> None:
        with input_path.open("wb") as output_file:
            while chunk := await file.read(1024 * 1024):
                output_file.write(chunk)

        if input_path.stat().st_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded audio file is empty.",
            )
