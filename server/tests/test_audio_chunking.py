import subprocess

import pytest
from fastapi import HTTPException

from services.audio_chunking import (
    AudioChunkingService,
    build_chunk_plan,
    calculate_chunk_seconds,
)


def test_calculate_chunk_seconds_clamps_by_duration() -> None:
    assert calculate_chunk_seconds(300, concurrency=3, min_seconds=300, max_seconds=900) == 300
    assert calculate_chunk_seconds(1800, concurrency=3, min_seconds=300, max_seconds=900) == 600
    assert calculate_chunk_seconds(5400, concurrency=3, min_seconds=300, max_seconds=900) == 900


def test_calculate_chunk_seconds_rejects_invalid_settings() -> None:
    with pytest.raises(ValueError):
        calculate_chunk_seconds(300, concurrency=0, min_seconds=300, max_seconds=900)

    with pytest.raises(ValueError):
        calculate_chunk_seconds(300, concurrency=3, min_seconds=900, max_seconds=300)


def test_build_chunk_plan_adds_overlap_without_extending_past_duration() -> None:
    plans = build_chunk_plan(duration_seconds=605, chunk_seconds=300, overlap_seconds=2)

    assert [(plan.index, plan.start_seconds, plan.duration_seconds) for plan in plans] == [
        (0, 0.0, 302.0),
        (1, 298.0, 304.0),
        (2, 598.0, 7.0),
    ]
    assert [plan.leading_overlap_seconds for plan in plans] == [0.0, 2.0, 2.0]


def test_create_chunks_uses_mono_16khz_mp3_settings(monkeypatch, tmp_path) -> None:
    input_path = tmp_path / "input.wav"
    input_path.write_bytes(b"fake audio")
    output_dir = tmp_path / "chunks"
    plans = build_chunk_plan(duration_seconds=300, chunk_seconds=300, overlap_seconds=2)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        output_path = tmp_path / "chunks" / "chunk_0000.mp3"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake mp3")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    chunks = AudioChunkingService(ffmpeg_path="ffmpeg").create_chunks(
        input_path=input_path,
        output_dir=output_dir,
        plans=plans,
        target_max_mb=24,
    )

    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].path == output_dir / "chunk_0000.mp3"
    assert "-ac" in calls[0]
    assert calls[0][calls[0].index("-ac") + 1] == "1"
    assert "-ar" in calls[0]
    assert calls[0][calls[0].index("-ar") + 1] == "16000"
    assert "-b:a" in calls[0]


def test_create_chunks_raises_when_size_limit_cannot_be_met(monkeypatch, tmp_path) -> None:
    input_path = tmp_path / "input.wav"
    input_path.write_bytes(b"fake audio")
    output_dir = tmp_path / "chunks"
    plans = build_chunk_plan(duration_seconds=300, chunk_seconds=300, overlap_seconds=0)

    def fake_run(args, **kwargs):
        output_path = tmp_path / "chunks" / "chunk_0000.mp3"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"x" * 2 * 1024 * 1024)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(HTTPException) as exc_info:
        AudioChunkingService(ffmpeg_path="ffmpeg").create_chunks(
            input_path=input_path,
            output_dir=output_dir,
            plans=plans,
            target_max_mb=1,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Audio chunk 0 could not be created under size limit."
