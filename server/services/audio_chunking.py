from dataclasses import dataclass
from math import ceil
from pathlib import Path
import subprocess

import imageio_ffmpeg
from fastapi import HTTPException, status


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    start_seconds: float
    duration_seconds: float
    leading_overlap_seconds: float


@dataclass(frozen=True)
class AudioChunk:
    index: int
    path: Path
    leading_overlap_seconds: float


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def calculate_chunk_seconds(
    duration_seconds: float,
    concurrency: int,
    min_seconds: int,
    max_seconds: int,
) -> int:
    if concurrency <= 0:
        raise ValueError("concurrency must be greater than zero")
    if min_seconds <= 0:
        raise ValueError("min_seconds must be greater than zero")
    if max_seconds < min_seconds:
        raise ValueError("max_seconds must be greater than or equal to min_seconds")
    if duration_seconds <= 0:
        return min_seconds
    return clamp(ceil(duration_seconds / concurrency), min_seconds, max_seconds)


def build_chunk_plan(
    duration_seconds: float,
    chunk_seconds: int,
    overlap_seconds: int,
) -> list[ChunkPlan]:
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be greater than zero")
    if overlap_seconds < 0:
        raise ValueError("overlap_seconds must be greater than or equal to zero")
    if duration_seconds <= 0:
        return []

    plans: list[ChunkPlan] = []
    index = 0
    base_start = 0.0

    while base_start < duration_seconds:
        leading_overlap = float(overlap_seconds if index > 0 else 0)
        start = max(0.0, base_start - leading_overlap)
        base_end = min(duration_seconds, base_start + chunk_seconds)
        end = min(duration_seconds, base_end + overlap_seconds)

        plans.append(
            ChunkPlan(
                index=index,
                start_seconds=start,
                duration_seconds=end - start,
                leading_overlap_seconds=leading_overlap,
            )
        )

        index += 1
        base_start += chunk_seconds

    return plans


class AudioChunkingService:
    def __init__(self, ffmpeg_path: str | None = None) -> None:
        self._ffmpeg_path = ffmpeg_path or imageio_ffmpeg.get_ffmpeg_exe()

    def create_chunks(
        self,
        input_path: Path,
        output_dir: Path,
        plans: list[ChunkPlan],
        target_max_mb: int,
    ) -> list[AudioChunk]:
        if not plans:
            return []
        if target_max_mb <= 0:
            raise ValueError("target_max_mb must be greater than zero")

        output_dir.mkdir(parents=True, exist_ok=True)
        chunks: list[AudioChunk] = []

        for plan in plans:
            output_path = output_dir / f"chunk_{plan.index:04d}.mp3"
            self._create_chunk(input_path, output_path, plan, target_max_mb)
            chunks.append(
                AudioChunk(
                    index=plan.index,
                    path=output_path,
                    leading_overlap_seconds=plan.leading_overlap_seconds,
                )
            )

        return chunks

    def _create_chunk(
        self,
        input_path: Path,
        output_path: Path,
        plan: ChunkPlan,
        target_max_mb: int,
    ) -> None:
        bitrates = self._candidate_bitrates(plan.duration_seconds, target_max_mb)
        target_max_bytes = target_max_mb * 1024 * 1024

        for bitrate_kbps in bitrates:
            completed = subprocess.run(
                [
                    self._ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{plan.start_seconds:.3f}",
                    "-t",
                    f"{plan.duration_seconds:.3f}",
                    "-i",
                    str(input_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-b:a",
                    f"{bitrate_kbps}k",
                    str(output_path),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            if completed.returncode != 0:
                continue
            if not output_path.exists() or output_path.stat().st_size == 0:
                continue
            if output_path.stat().st_size <= target_max_bytes:
                return

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Audio chunk {plan.index} could not be created under size limit.",
        )

    def _candidate_bitrates(
        self,
        duration_seconds: float,
        target_max_mb: int,
    ) -> list[int]:
        if duration_seconds <= 0:
            return [64, 48, 32, 24, 16]

        target_bits = target_max_mb * 1024 * 1024 * 8
        max_kbps = max(16, int((target_bits / duration_seconds / 1000) * 0.95))
        first = min(64, max_kbps)
        candidates = [first, 48, 32, 24, 16]
        return sorted({bitrate for bitrate in candidates if bitrate <= first}, reverse=True)
