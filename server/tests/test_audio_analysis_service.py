import subprocess

import pytest
from fastapi import HTTPException

from services.audio_analysis_service import AudioAnalysisService


def test_parse_duration_seconds() -> None:
    service = AudioAnalysisService(ffmpeg_path="ffmpeg")

    duration = service._parse_duration_seconds(
        "Duration: 01:02:03.45, start: 0.000000, bitrate: 128 kb/s"
    )

    assert duration == 3723.45


def test_analyze_rejects_empty_file(tmp_path) -> None:
    service = AudioAnalysisService(ffmpeg_path="ffmpeg")
    audio_path = tmp_path / "empty.wav"
    audio_path.write_bytes(b"")

    with pytest.raises(HTTPException) as exc_info:
        service.analyze(audio_path)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Uploaded audio file is empty."


def test_analyze_rejects_undecodable_file(monkeypatch, tmp_path) -> None:
    service = AudioAnalysisService(ffmpeg_path="ffmpeg")
    audio_path = tmp_path / "broken.wav"
    audio_path.write_bytes(b"not audio")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="Invalid data")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(HTTPException) as exc_info:
        service.analyze(audio_path)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Uploaded audio file could not be decoded."


def test_analyze_returns_duration(monkeypatch, tmp_path) -> None:
    service = AudioAnalysisService(ffmpeg_path="ffmpeg")
    audio_path = tmp_path / "recording.wav"
    audio_path.write_bytes(b"fake audio")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="Duration: 00:05:10.25, start: 0.000000, bitrate: 64 kb/s",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    analysis = service.analyze(audio_path)

    assert analysis.duration_seconds == 310.25
