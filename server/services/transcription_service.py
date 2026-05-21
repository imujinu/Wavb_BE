import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class TranscriptionSegment:
    text: str
    start_seconds: float | None
    end_seconds: float | None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    duration_seconds: float | None
    stt_model: str
    segments: list[TranscriptionSegment]


@dataclass(frozen=True)
class ChunkTranscription:
    index: int
    text: str
    leading_overlap_seconds: float
    start_seconds: float
    duration_seconds: float
    segments: list[TranscriptionSegment]


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
        result = await self.transcribe_with_segments(file)
        return result.text

    # Transcribe audio and keep timing data for transcript/segment persistence.
    async def transcribe_with_segments(self, file: UploadFile) -> TranscriptionResult:
        suffix = Path(file.filename or "").suffix or ".audio"

        try:
            with tempfile.TemporaryDirectory() as temp_dir_name:
                input_path = Path(temp_dir_name) / f"input{suffix}"
                await self._save_upload(file, input_path)
                analysis = self._audio_analysis_service.analyze(input_path)
                chunk_seconds = calculate_chunk_seconds(
                    duration_seconds=analysis.duration_seconds,
                    concurrency=self._transcription_concurrency,
                    min_seconds=self._chunk_min_seconds,
                    max_seconds=self._chunk_max_seconds,
                )
                chunk_plans = build_chunk_plan(
                    duration_seconds=analysis.duration_seconds,
                    chunk_seconds=chunk_seconds,
                    overlap_seconds=self._chunk_overlap_seconds,
                )
                chunks = self._audio_chunking_service.create_chunks(
                    input_path=input_path,
                    output_dir=Path(temp_dir_name) / "chunks",
                    plans=chunk_plans,
                    target_max_mb=self._target_chunk_max_mb,
                )
                transcriptions = await self._collect_chunk_transcriptions(chunks)
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

        return TranscriptionResult(
            text=self._merge_chunk_transcriptions(transcriptions).strip(),
            duration_seconds=analysis.duration_seconds,
            stt_model=self._model,
            segments=self._merge_transcription_segments(transcriptions),
        )

    async def _transcribe_chunks(self, chunks: list[AudioChunk]) -> str:
        transcriptions = await self._collect_chunk_transcriptions(chunks)
        return self._merge_chunk_transcriptions(transcriptions)

    
    async def _collect_chunk_transcriptions(
        self,
        chunks: list[AudioChunk],
    ) -> list[ChunkTranscription]:
        if not chunks:
            return []

        semaphore = asyncio.Semaphore(self._transcription_concurrency)
        tasks = [
            asyncio.create_task(self._transcribe_chunk_with_retry(chunk, semaphore))
            for chunk in chunks
        ]

        try:
            return await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise

    async def _transcribe_chunk_with_retry(
        self,
        chunk: AudioChunk,
        semaphore: asyncio.Semaphore,
    ) -> ChunkTranscription:
        last_exception: Exception | None = None

        for _ in range(2):
            try:
                async with semaphore:
                    response = await self._request_chunk_transcription(chunk)
                return self._parse_chunk_transcription(chunk, response)
            except Exception as exc:
                last_exception = exc

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Audio transcription provider failed for chunk {chunk.index}.",
        ) from last_exception

    async def _request_chunk_transcription(self, chunk: AudioChunk) -> Any:
        with chunk.path.open("rb") as audio_file:
            return await self._client.audio.transcriptions.create(
                model=self._model,
                file=audio_file,
                language="ko",
                response_format="verbose_json",
            )

    def _parse_chunk_transcription(
        self,
        chunk: AudioChunk,
        response: Any,
    ) -> ChunkTranscription:
        text = str(self._get_response_value(response, "text", "") or "").strip()
        raw_segments = self._get_response_value(response, "segments", []) or []
        segments = [
            TranscriptionSegment(
                text=str(self._get_response_value(segment, "text", "") or "").strip(),
                start_seconds=self._to_float_or_none(
                    self._get_response_value(segment, "start", None)
                ),
                end_seconds=self._to_float_or_none(
                    self._get_response_value(segment, "end", None)
                ),
            )
            for segment in raw_segments
        ]

        return ChunkTranscription(
            index=chunk.index,
            text=text,
            leading_overlap_seconds=chunk.leading_overlap_seconds,
            start_seconds=chunk.start_seconds,
            duration_seconds=chunk.duration_seconds,
            segments=segments,
        )

    def _merge_chunk_transcriptions(
        self,
        transcripts: list[ChunkTranscription],
    ) -> str:
        merged: list[str] = []

        for transcript in sorted(transcripts, key=lambda item: item.index):
            if transcript.segments:
                segment_text = self._merge_segments(transcript)
                if segment_text:
                    merged.append(segment_text)
                continue

            if transcript.text:
                merged.append(transcript.text)

        return "\n".join(transcript for transcript in merged if transcript)

    def _merge_transcription_segments(
        self,
        transcripts: list[ChunkTranscription],
    ) -> list[TranscriptionSegment]:
        segments: list[TranscriptionSegment] = []

        for transcript in sorted(transcripts, key=lambda item: item.index):
            if not transcript.segments and transcript.text:
                segments.append(
                    TranscriptionSegment(
                        text=transcript.text,
                        start_seconds=transcript.start_seconds,
                        end_seconds=transcript.start_seconds + transcript.duration_seconds,
                    )
                )
                continue

            for segment in transcript.segments:
                if not segment.text:
                    continue
                if (
                    transcript.leading_overlap_seconds > 0
                    and segment.end_seconds is not None
                    and segment.end_seconds <= transcript.leading_overlap_seconds
                ):
                    continue
                segments.append(
                    TranscriptionSegment(
                        text=segment.text,
                        start_seconds=self._offset_seconds(
                            transcript.start_seconds,
                            segment.start_seconds,
                        ),
                        end_seconds=self._offset_seconds(
                            transcript.start_seconds,
                            segment.end_seconds,
                        ),
                    )
                )

        return segments

    def _merge_segments(self, transcript: ChunkTranscription) -> str:
        texts: list[str] = []

        for segment in transcript.segments:
            if not segment.text:
                continue
            if (
                transcript.leading_overlap_seconds > 0
                and segment.end_seconds is not None
                and segment.end_seconds <= transcript.leading_overlap_seconds
            ):
                continue
            texts.append(segment.text)

        return " ".join(texts).strip()

    def _get_response_value(self, response: Any, key: str, default: Any) -> Any:
        if isinstance(response, dict):
            return response.get(key, default)
        return getattr(response, key, default)

    def _offset_seconds(self, base_seconds: float, value: float | None) -> float | None:
        if value is None:
            return None
        return base_seconds + value

    def _to_float_or_none(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _save_upload(self, file: UploadFile, input_path: Path) -> None:
        with input_path.open("wb") as output_file:
            while chunk := await file.read(1024 * 1024):
                output_file.write(chunk)

        if input_path.stat().st_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded audio file is empty.",
            )
