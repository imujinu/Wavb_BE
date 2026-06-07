import asyncio
import csv
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from services.audio.audio_analysis_service import AudioAnalysisService
from services.audio.audio_chunking import AudioChunkingService, build_chunk_plan
from services.audio.transcription_service import (
    ChunkTranscription,
    TranscriptionService,
)
from settings import get_settings


AUDIO_FILE_PATHS = {
    "30m": SERVER_ROOT / "assets" / "30m.mp3",
}
CHUNK_SECONDS_OPTIONS = [300, 600]
CONCURRENCY_OPTIONS = [1,2,3]
TARGET_SECONDS = 180
OUTPUT_CSV_PATH = SERVER_ROOT / "audio_stt_benchmark.csv"

RESULT_COLUMNS = [
    "file_label",
    "file_path",
    "audio_duration_seconds",
    "chunk_seconds",
    "concurrency",
    "status",
    "total_seconds",
    "chunk_count",
    "chunk_create_seconds",
    "stt_wall_seconds",
    "rate_limit_error_count",
    "failed_attempt_count",
    "retry_count",
    "transcript_length",
    "estimated_cost_usd",
    "within_target",
    "error",
]


@dataclass(frozen=True)
class BenchmarkResult:
    file_label: str
    file_path: Path
    audio_duration_seconds: float
    chunk_seconds: int
    concurrency: int
    status: str
    total_seconds: float
    chunk_count: int
    chunk_create_seconds: float
    stt_wall_seconds: float
    rate_limit_error_count: int
    failed_attempt_count: int
    retry_count: int
    transcript_length: int
    estimated_cost_usd: float
    within_target: bool
    error: str


class InstrumentedTranscriptionService(TranscriptionService):
    def __init__(self, concurrency: int) -> None:
        super().__init__()
        self._transcription_concurrency = concurrency
        self.attempt_count = 0
        self.failed_attempt_count = 0
        self.rate_limit_error_count = 0
        self.stt_request_seconds_total = 0.0

    async def _request_chunk_transcription(self, chunk, language: str = "ko") -> Any:
        self.attempt_count += 1
        started_at = time.perf_counter()
        try:
            return await super()._request_chunk_transcription(chunk, language)
        except Exception as exc:
            self.failed_attempt_count += 1
            if is_rate_limit_error(exc):
                self.rate_limit_error_count += 1
            raise
        finally:
            self.stt_request_seconds_total += time.perf_counter() - started_at


def is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code == 429:
        return True
    exc_name = type(exc).__name__.lower()
    message = str(exc).lower()
    return "ratelimit" in exc_name or "rate limit" in message


def estimate_cost_usd(duration_seconds: float) -> float:
    return round((duration_seconds / 60.0) * 0.006, 6)


def make_result_row(result: BenchmarkResult) -> dict[str, str]:
    values = {
        "file_label": result.file_label,
        "file_path": str(result.file_path),
        "audio_duration_seconds": _format_number(result.audio_duration_seconds),
        "chunk_seconds": str(result.chunk_seconds),
        "concurrency": str(result.concurrency),
        "status": result.status,
        "total_seconds": _format_number(result.total_seconds),
        "chunk_count": str(result.chunk_count),
        "chunk_create_seconds": _format_number(result.chunk_create_seconds),
        "stt_wall_seconds": _format_number(result.stt_wall_seconds),
        "rate_limit_error_count": str(result.rate_limit_error_count),
        "failed_attempt_count": str(result.failed_attempt_count),
        "retry_count": str(result.retry_count),
        "transcript_length": str(result.transcript_length),
        "estimated_cost_usd": _format_number(result.estimated_cost_usd),
        "within_target": str(result.within_target).lower(),
        "error": result.error,
    }
    return {column: values[column] for column in RESULT_COLUMNS}


def render_table(results: Iterable[BenchmarkResult]) -> str:
    rows = [make_result_row(result) for result in results]
    widths = {
        column: max(len(column), *(len(row[column]) for row in rows))
        for column in RESULT_COLUMNS
    }
    header = " | ".join(column.ljust(widths[column]) for column in RESULT_COLUMNS)
    divider = "-+-".join("-" * widths[column] for column in RESULT_COLUMNS)
    body = [
        " | ".join(row[column].ljust(widths[column]) for column in RESULT_COLUMNS)
        for row in rows
    ]
    return "\n".join([header, divider, *body])


def write_csv(results: Iterable[BenchmarkResult], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow(make_result_row(result))


async def run_benchmark_matrix(
    audio_file_paths: Mapping[str, Path] = AUDIO_FILE_PATHS,
    chunk_seconds_options: Iterable[int] = CHUNK_SECONDS_OPTIONS,
    concurrency_options: Iterable[int] = CONCURRENCY_OPTIONS,
    target_seconds: int = TARGET_SECONDS,
    output_csv_path: Path = OUTPUT_CSV_PATH,
    language: str = "ko",
    audio_analysis_service: AudioAnalysisService | None = None,
    chunking_service: AudioChunkingService | None = None,
    transcription_service_factory: Callable[[int], TranscriptionService] | None = None,
    print_results: bool = True,
) -> list[BenchmarkResult]:
    analysis_service = audio_analysis_service or AudioAnalysisService()
    chunk_service = chunking_service or AudioChunkingService()
    service_factory = transcription_service_factory or InstrumentedTranscriptionService
    settings = get_settings()
    results: list[BenchmarkResult] = []

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir_name:
        temp_root = Path(temp_dir_name)
        for file_label, file_path in audio_file_paths.items():
            file_path = Path(file_path)
            try:
                duration_seconds = analysis_service.analyze(file_path).duration_seconds
            except Exception as exc:
                results.extend(
                    _failed_analysis_results(
                        file_label=file_label,
                        file_path=file_path,
                        chunk_seconds_options=chunk_seconds_options,
                        concurrency_options=concurrency_options,
                        error=str(exc),
                        target_seconds=target_seconds,
                    )
                )
                continue

            for chunk_seconds in chunk_seconds_options:
                for concurrency in concurrency_options:
                    if duration_seconds <= chunk_seconds:
                        _log_skipped(file_label, chunk_seconds, concurrency)
                        results.append(
                            _skipped_result(
                                file_label=file_label,
                                file_path=file_path,
                                duration_seconds=duration_seconds,
                                chunk_seconds=chunk_seconds,
                                concurrency=concurrency,
                                target_seconds=target_seconds,
                            )
                        )
                        continue

                    result = await run_single_benchmark(
                        file_label=file_label,
                        file_path=file_path,
                        duration_seconds=duration_seconds,
                        chunk_seconds=chunk_seconds,
                        concurrency=concurrency,
                        target_seconds=target_seconds,
                        work_dir=temp_root / file_label / f"{chunk_seconds}s_{concurrency}c",
                        chunking_service=chunk_service,
                        transcription_service=service_factory(concurrency),
                        target_chunk_max_mb=settings.audio_target_chunk_max_mb,
                        overlap_seconds=settings.audio_chunk_overlap_seconds,
                        language=language,
                    )
                    results.append(result)

    write_csv(results, output_csv_path)
    if print_results:
        print(render_table(results))
        print(f"\nCSV saved to: {output_csv_path}")
    return results


async def run_single_benchmark(
    file_label: str,
    file_path: Path,
    duration_seconds: float,
    chunk_seconds: int,
    concurrency: int,
    target_seconds: int,
    work_dir: Path,
    chunking_service: AudioChunkingService,
    transcription_service: TranscriptionService,
    target_chunk_max_mb: int,
    overlap_seconds: int,
    language: str = "ko",
) -> BenchmarkResult:
    total_started_at = time.perf_counter()
    chunk_create_seconds = 0.0
    stt_wall_seconds = 0.0
    chunk_count = 0
    transcript_length = 0

    _log_benchmark_start(file_label, chunk_seconds, concurrency)

    try:
        plans = build_chunk_plan(
            duration_seconds=duration_seconds,
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
        )

        chunk_started_at = time.perf_counter()
        chunks = chunking_service.create_chunks(
            input_path=file_path,
            output_dir=work_dir / "chunks",
            plans=plans,
            target_max_mb=target_chunk_max_mb,
        )
        chunk_create_seconds = time.perf_counter() - chunk_started_at
        chunk_count = len(chunks)

        stt_started_at = time.perf_counter()
        transcriptions = await transcription_service._collect_chunk_transcriptions(
            chunks,
            language=language,
        )
        stt_wall_seconds = time.perf_counter() - stt_started_at
        transcript = _merge_transcript(transcription_service, transcriptions)
        transcript_length = len(transcript)

        total_seconds = time.perf_counter() - total_started_at
        result = BenchmarkResult(
            file_label=file_label,
            file_path=file_path,
            audio_duration_seconds=duration_seconds,
            chunk_seconds=chunk_seconds,
            concurrency=concurrency,
            status="ok",
            total_seconds=total_seconds,
            chunk_count=chunk_count,
            chunk_create_seconds=chunk_create_seconds,
            stt_wall_seconds=stt_wall_seconds,
            rate_limit_error_count=_metric(transcription_service, "rate_limit_error_count"),
            failed_attempt_count=_metric(transcription_service, "failed_attempt_count"),
            retry_count=_retry_count(transcription_service, chunk_count),
            transcript_length=transcript_length,
            estimated_cost_usd=estimate_cost_usd(duration_seconds),
            within_target=total_seconds <= target_seconds,
            error="",
        )
        _log_benchmark_done(result)
        return result
    except Exception as exc:
        total_seconds = time.perf_counter() - total_started_at
        result = BenchmarkResult(
            file_label=file_label,
            file_path=file_path,
            audio_duration_seconds=duration_seconds,
            chunk_seconds=chunk_seconds,
            concurrency=concurrency,
            status="failed",
            total_seconds=total_seconds,
            chunk_count=chunk_count,
            chunk_create_seconds=chunk_create_seconds,
            stt_wall_seconds=stt_wall_seconds,
            rate_limit_error_count=_metric(transcription_service, "rate_limit_error_count"),
            failed_attempt_count=_metric(transcription_service, "failed_attempt_count"),
            retry_count=_retry_count(transcription_service, chunk_count),
            transcript_length=transcript_length,
            estimated_cost_usd=estimate_cost_usd(duration_seconds),
            within_target=False,
            error=str(exc),
        )
        _log_benchmark_done(result)
        return result


def _merge_transcript(
    transcription_service: TranscriptionService,
    transcriptions: list[ChunkTranscription],
) -> str:
    merge = getattr(transcription_service, "_merge_chunk_transcriptions", None)
    if callable(merge):
        return str(merge(transcriptions)).strip()
    return "\n".join(item.text for item in transcriptions if item.text).strip()


def _metric(transcription_service: TranscriptionService, name: str) -> int:
    return int(getattr(transcription_service, name, 0) or 0)


def _retry_count(transcription_service: TranscriptionService, chunk_count: int) -> int:
    attempt_count = int(getattr(transcription_service, "attempt_count", chunk_count) or 0)
    return max(0, attempt_count - chunk_count)


def _log_benchmark_start(
    file_label: str,
    chunk_seconds: int,
    concurrency: int,
) -> None:
    print(f"[{file_label}] chunk={chunk_seconds}s concurrency={concurrency} 시작...")


def _log_benchmark_done(result: BenchmarkResult) -> None:
    elapsed = f"{result.total_seconds:.1f}초"
    prefix = (
        f"[{result.file_label}] chunk={result.chunk_seconds}s "
        f"concurrency={result.concurrency} → {elapsed}"
    )
    if result.status == "ok" and result.within_target:
        print(f"{prefix} ✅ (within target)")
        return
    print(
        f"{prefix} ❌ rate_limit={result.rate_limit_error_count} "
        f"retry={result.retry_count}"
    )


def _log_skipped(file_label: str, chunk_seconds: int, concurrency: int) -> None:
    print(f"[{file_label}] chunk={chunk_seconds}s concurrency={concurrency} → skipped")


def _skipped_result(
    file_label: str,
    file_path: Path,
    duration_seconds: float,
    chunk_seconds: int,
    concurrency: int,
    target_seconds: int,
) -> BenchmarkResult:
    return BenchmarkResult(
        file_label=file_label,
        file_path=file_path,
        audio_duration_seconds=duration_seconds,
        chunk_seconds=chunk_seconds,
        concurrency=concurrency,
        status="skipped",
        total_seconds=0.0,
        chunk_count=0,
        chunk_create_seconds=0.0,
        stt_wall_seconds=0.0,
        rate_limit_error_count=0,
        failed_attempt_count=0,
        retry_count=0,
        transcript_length=0,
        estimated_cost_usd=estimate_cost_usd(duration_seconds),
        within_target=False,
        error=f"audio duration <= chunk_seconds; target={target_seconds}s",
    )


def _failed_analysis_results(
    file_label: str,
    file_path: Path,
    chunk_seconds_options: Iterable[int],
    concurrency_options: Iterable[int],
    error: str,
    target_seconds: int,
) -> list[BenchmarkResult]:
    return [
        BenchmarkResult(
            file_label=file_label,
            file_path=file_path,
            audio_duration_seconds=0.0,
            chunk_seconds=chunk_seconds,
            concurrency=concurrency,
            status="failed",
            total_seconds=0.0,
            chunk_count=0,
            chunk_create_seconds=0.0,
            stt_wall_seconds=0.0,
            rate_limit_error_count=0,
            failed_attempt_count=0,
            retry_count=0,
            transcript_length=0,
            estimated_cost_usd=0.0,
            within_target=False,
            error=f"audio analysis failed: {error}; target={target_seconds}s",
        )
        for chunk_seconds in chunk_seconds_options
        for concurrency in concurrency_options
    ]


def _format_number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def main() -> None:
    asyncio.run(run_benchmark_matrix())


if __name__ == "__main__":
    main()
