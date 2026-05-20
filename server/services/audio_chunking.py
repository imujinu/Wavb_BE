from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    start_seconds: float
    duration_seconds: float
    leading_overlap_seconds: float


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def calculate_chunk_seconds(
    duration_seconds: float,
    concurrency: int,
    min_seconds: int,
    max_seconds: int,
) -> int:
    if duration_seconds <= 0:
        return min_seconds
    return clamp(ceil(duration_seconds / concurrency), min_seconds, max_seconds)


def build_chunk_plan(
    duration_seconds: float,
    chunk_seconds: int,
    overlap_seconds: int,
) -> list[ChunkPlan]:
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
