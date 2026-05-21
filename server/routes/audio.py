from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from db.connection import get_database
from repositories.rag_repository import RagRepository
from schemas.rag import DomainType
from services.summary_service import SummaryService
from services.transcript_ingestion_service import TranscriptIngestionService
from services.transcription_service import TranscriptionService


router = APIRouter(prefix="/audio", tags=["audio"])

ALLOWED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm"}


class AudioSummaryResponse(BaseModel):
    transcript: str
    summary: str


class AudioTranscriptResponse(BaseModel):
    transcript_id: UUID
    transcript: str
    duration_seconds: float | None
    stt_model: str
    segment_count: int
    status: str


async def get_rag_repository() -> AsyncIterator[RagRepository]:
    database = get_database()
    await database.connect()
    try:
        async with database.pool.acquire() as connection:
            yield RagRepository(connection)
    finally:
        await database.disconnect()


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


@router.post("/transcripts", response_model=AudioTranscriptResponse)
async def create_audio_transcript(
    file: UploadFile = File(...),
    domain_type: DomainType = Form("meeting"),
    title: str | None = Form(None),
    user_id: UUID | None = Form(None),
    repository: RagRepository = Depends(get_rag_repository),
) -> AudioTranscriptResponse:
    validate_audio_file(file)

    ingestion_service = TranscriptIngestionService(repository)
    result = await ingestion_service.ingest_upload(
        file=file,
        domain_type=domain_type,
        title=title,
        user_id=user_id,
    )
    return AudioTranscriptResponse(
        transcript_id=result.transcript_id,
        transcript=result.transcript,
        duration_seconds=result.duration_seconds,
        stt_model=result.stt_model,
        segment_count=result.segment_count,
        status="completed",
    )
