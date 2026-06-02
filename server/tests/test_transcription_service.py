import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from services.audio.audio_chunking import AudioChunk
from services.audio.transcription_service import TranscriptionService


class FakeTranscriptions:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        return await self._handler(**kwargs)


class FakeAudio:
    def __init__(self, handler):
        self.transcriptions = FakeTranscriptions(handler)


class FakeClient:
    def __init__(self, handler):
        self.audio = FakeAudio(handler)


def make_service(handler, concurrency: int = 2) -> TranscriptionService:
    service = TranscriptionService.__new__(TranscriptionService)
    service._client = FakeClient(handler)
    service._model = "whisper-1"
    service._transcription_concurrency = concurrency
    return service


def write_chunk(tmp_path: Path, index: int) -> AudioChunk:
    path = tmp_path / f"chunk_{index:04d}.mp3"
    path.write_bytes(b"fake mp3")
    return AudioChunk(index=index, path=path, leading_overlap_seconds=0)


@pytest.mark.asyncio
async def test_transcribe_chunks_limits_concurrency_and_keeps_chunk_order(tmp_path) -> None:
    active_count = 0
    max_active_count = 0

    async def handler(**kwargs):
        nonlocal active_count, max_active_count
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        file_name = Path(kwargs["file"].name).stem
        chunk_index = int(file_name.rsplit("_", 1)[-1])
        await asyncio.sleep(0.02 if chunk_index == 0 else 0.001)
        active_count -= 1
        return {"text": f"chunk {chunk_index}", "segments": []}

    service = make_service(handler, concurrency=2)
    chunks = [write_chunk(tmp_path, index) for index in range(4)]

    transcript = await service._transcribe_chunks(chunks)

    assert max_active_count <= 2
    assert transcript == "chunk 0\nchunk 1\nchunk 2\nchunk 3"


@pytest.mark.asyncio
async def test_transcribe_chunk_retries_once_before_success(tmp_path) -> None:
    attempts = 0

    async def handler(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary provider failure")
        return {"text": "retry success", "segments": []}

    service = make_service(handler)
    chunk = write_chunk(tmp_path, 0)

    transcript = await service._transcribe_chunks([chunk])

    assert attempts == 2
    assert transcript == "retry success"


@pytest.mark.asyncio
async def test_transcribe_chunk_failure_returns_502_with_chunk_index(tmp_path) -> None:
    attempts = 0

    async def handler(**kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("provider failure")

    service = make_service(handler)
    chunk = write_chunk(tmp_path, 7)

    with pytest.raises(HTTPException) as exc_info:
        await service._transcribe_chunks([chunk])

    assert attempts == 2
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Audio transcription provider failed for chunk 7."


@pytest.mark.asyncio
async def test_transcribe_chunks_uses_verbose_json_segments_to_skip_leading_overlap(
    tmp_path,
) -> None:
    async def handler(**kwargs):
        file_name = Path(kwargs["file"].name).stem
        chunk_index = int(file_name.rsplit("_", 1)[-1])
        assert kwargs["response_format"] == "verbose_json"
        if chunk_index == 0:
            return {
                "text": "first second",
                "segments": [
                    {"text": "first", "start": 0.0, "end": 1.0},
                    {"text": "second", "start": 1.0, "end": 2.0},
                ],
            }
        return {
            "text": "duplicate third",
            "segments": [
                {"text": "duplicate", "start": 0.0, "end": 1.5},
                {"text": "third", "start": 1.5, "end": 3.0},
            ],
        }

    service = make_service(handler)
    first = write_chunk(tmp_path, 0)
    second_path = tmp_path / "chunk_0001.mp3"
    second_path.write_bytes(b"fake mp3")
    second = AudioChunk(index=1, path=second_path, leading_overlap_seconds=2)

    transcript = await service._transcribe_chunks([second, first])

    assert transcript == "first second\nthird"
