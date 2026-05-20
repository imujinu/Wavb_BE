import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from openai import APIError, AsyncOpenAI

from services.audio_analysis_service import AudioAnalysisService
from services.audio_chunking import (
    AudioChunk,
    AudioChunkingService,
    build_chunk_plan,
    calculate_chunk_seconds,
)
from settings import get_settings

#todo : Exception handling 공통화하기
# 오디오 -> 스크립트로 변환 서비스
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
        self._audio_chunking_service = AudioChunkingService()
        self._transcription_concurrency = settings.audio_transcription_concurrency
        self._chunk_min_seconds = settings.audio_chunk_min_seconds
        self._chunk_max_seconds = settings.audio_chunk_max_seconds
        self._chunk_overlap_seconds = settings.audio_chunk_overlap_seconds
        self._target_chunk_max_mb = settings.audio_target_chunk_max_mb

    async def transcribe(self, file: UploadFile) -> str:
        suffix = Path(file.filename or "").suffix or ".audio"

        try:
            with tempfile.TemporaryDirectory() as temp_dir_name:
                input_path = Path(temp_dir_name) / f"input{suffix}"
                await self._save_upload(file, input_path)
                analysis = self._audio_analysis_service.analyze(input_path)
                # 청킹 단위를 min~max 넘어서면 범위 안으로 조정
                chunk_seconds = calculate_chunk_seconds(
                    duration_seconds=analysis.duration_seconds,
                    concurrency=self._transcription_concurrency,
                    min_seconds=self._chunk_min_seconds,
                    max_seconds=self._chunk_max_seconds,
                )
                # 위에서 정한 청킹 사이즈만큼 영상을 나눔
                chunk_plans = build_chunk_plan(
                    duration_seconds=analysis.duration_seconds,
                    chunk_seconds=chunk_seconds,
                    overlap_seconds=self._chunk_overlap_seconds,
                )
                # 실제로 청크 생성
                chunks = self._audio_chunking_service.create_chunks(
                    input_path=input_path,
                    output_dir=Path(temp_dir_name) / "chunks",
                    plans=chunk_plans,
                    target_max_mb=self._target_chunk_max_mb,
                )
                transcription = await self._transcribe_chunks(chunks)
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

# chunck를 실제 script로 변환
    async def _transcribe_chunks(self, chunks: list[AudioChunk]) -> str:
        transcripts: list[tuple[int, str]] = []

        for chunk in chunks:
            with chunk.path.open("rb") as audio_file:
                transcription = await self._client.audio.transcriptions.create(
                    model=self._model,
                    file=audio_file,
                    language="ko",
                    response_format="text",
                )
            transcripts.append((chunk.index, str(transcription).strip()))

        return "\n".join(
            transcript
            for _, transcript in sorted(transcripts, key=lambda item: item[0])
            if transcript
        )

    async def _save_upload(self, file: UploadFile, input_path: Path) -> None:
        with input_path.open("wb") as output_file:
            while chunk := await file.read(1024 * 1024):
                output_file.write(chunk)

        if input_path.stat().st_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded audio file is empty.",
            )
