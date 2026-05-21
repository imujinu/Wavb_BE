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
class ChunkTranscription:
    index: int
    text: str
    leading_overlap_seconds: float
    segments: list[TranscriptionSegment]

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
        if not chunks:
            return ""

        semaphore = asyncio.Semaphore(self._transcription_concurrency)
        tasks = [
            asyncio.create_task(self._transcribe_chunk_with_retry(chunk, semaphore))
            for chunk in chunks
        ]

        try:
            transcripts = await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise

        return "\n".join(
            transcript
            for transcript in self._merge_chunk_transcriptions(transcripts)
            if transcript
        )

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

    # 실질적으로 비동기 처리 요청을 실시하는 부분
    async def _request_chunk_transcription(self, chunk: AudioChunk) -> Any:
        with chunk.path.open("rb") as audio_file:
            return await self._client.audio.transcriptions.create(
                model=self._model,
                file=audio_file,
                language="ko",
                response_format="verbose_json",
            )
    # llm에게 받은 결과를 다시 클래스로 변환하는 과정
# llm이 청킹 단위로 텍스트를 반환하는데, 이 텍스트를 다시 합치는 과정에서 청킹 단위로 반환된 텍스트들을 다시 합치는 과정
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
            segments=segments,
        )
    #  chunk 단위로 변환된 텍스트들을 다시 합치는 과정
    # 청킹 단위로 반환된 텍스트들을 다시 합치는 과정에서, 청킹 단위로 반환된 텍스트들이 겹치는 부분이 있을 수 있기 때문에, 겹치는 부분을 제거하면서 텍스트를 합치는 과정
    def _merge_chunk_transcriptions(
        self,
        transcripts: list[ChunkTranscription],
    ) -> list[str]:
        merged: list[str] = []

        for transcript in sorted(transcripts, key=lambda item: item.index):
            if transcript.segments:
                segment_text = self._merge_segments(transcript)
                if segment_text:
                    merged.append(segment_text)
                continue

            if transcript.text:
                merged.append(transcript.text)

        return merged

    
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
