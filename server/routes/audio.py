from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from services.summary_service import SummaryService
from services.transcription_service import TranscriptionService


router = APIRouter(prefix="/audio", tags=["audio"])

ALLOWED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm"}


class AudioSummaryResponse(BaseModel):
    transcript: str
    summary: str


def validate_audio_file(file: UploadFile) -> None:
    filename = file.filename or ""
    suffix = f".{filename.rsplit('.', 1)[-1].lower()}" if "." in filename else ""

    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported audio file type. Allowed extensions: {allowed}",
        )


@router.post("/summarize", response_model=AudioSummaryResponse)
async def summarize_audio(file: UploadFile = File(...)) -> AudioSummaryResponse:
    validate_audio_file(file)

    transcription_service = TranscriptionService()
    summary_service = SummaryService()

    transcript = await transcription_service.transcribe(file)
    if not transcript.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Audio transcription result is empty.",
        )

    summary = await summary_service.summarize(transcript)
    return AudioSummaryResponse(transcript=transcript, summary=summary)
