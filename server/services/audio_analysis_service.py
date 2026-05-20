import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from fastapi import HTTPException, status


@dataclass(frozen=True)
class AudioAnalysis:
    duration_seconds: float

# ffmpeg를 사용해 오디오 길이 추출

class AudioAnalysisService:
    def __init__(self, ffmpeg_path: str | None = None) -> None:
        self._ffmpeg_path = ffmpeg_path or imageio_ffmpeg.get_ffmpeg_exe()

    def analyze(self, audio_path: Path) -> AudioAnalysis:
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded audio file is empty.",
            )

        completed = subprocess.run(
            [self._ffmpeg_path, "-hide_banner", "-i", str(audio_path)],
            capture_output=True,
            check=False,
            text=True,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        duration_seconds = self._parse_duration_seconds(output)

        if duration_seconds is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded audio file could not be decoded.",
            )

        return AudioAnalysis(duration_seconds=duration_seconds)

    def _parse_duration_seconds(self, ffmpeg_output: str) -> float | None:
        match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
            ffmpeg_output,
        )
        if not match:
            return None

        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
