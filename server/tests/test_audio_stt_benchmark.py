import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.audio.audio_chunking import AudioChunk, build_chunk_plan
from tools import audio_stt_benchmark as benchmark


class FakeAudioAnalysisService:
    def __init__(self, durations: dict[str, float]) -> None:
        self._durations = durations

    def analyze(self, path: Path) -> SimpleNamespace:
        return SimpleNamespace(duration_seconds=self._durations[path.name])


class FakeChunkingService:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, int]] = []

    def create_chunks(self, input_path, output_dir, plans, target_max_mb):
        self.calls.append((input_path, len(plans)))
        return [
            AudioChunk(
                index=plan.index,
                path=output_dir / f"chunk_{plan.index:04d}.mp3",
                leading_overlap_seconds=plan.leading_overlap_seconds,
                start_seconds=plan.start_seconds,
                duration_seconds=plan.duration_seconds,
            )
            for plan in plans
        ]


class FakeTranscriptionService:
    def __init__(self, concurrency: int, fail: bool = False) -> None:
        self.concurrency = concurrency
        self.fail = fail
        self.attempt_count = 0
        self.failed_attempt_count = 0
        self.rate_limit_error_count = 0

    async def _collect_chunk_transcriptions(self, chunks, language="ko"):
        self.attempt_count = len(chunks)
        if self.fail:
            self.attempt_count += 1
            self.failed_attempt_count = 1
            self.rate_limit_error_count = 1
            raise RuntimeError("rate limit")
        return [
            SimpleNamespace(index=chunk.index, text=f"chunk {chunk.index}")
            for chunk in chunks
        ]

    def _merge_chunk_transcriptions(self, transcriptions) -> str:
        return "\n".join(item.text for item in transcriptions)


def fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        audio_target_chunk_max_mb=24,
        audio_chunk_overlap_seconds=2,
    )


@pytest.mark.asyncio
async def test_benchmark_matrix_defaults_to_10m_cost_controlled_cases(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(benchmark, "get_settings", fake_settings)
    durations = {
        "10m.mp3": 600.0,
    }
    chunking_service = FakeChunkingService()

    results = await benchmark.run_benchmark_matrix(
        output_csv_path=tmp_path / "benchmark.csv",
        audio_analysis_service=FakeAudioAnalysisService(durations),
        chunking_service=chunking_service,
        transcription_service_factory=lambda concurrency: FakeTranscriptionService(
            concurrency
        ),
        print_results=False,
    )

    assert benchmark.AUDIO_FILE_PATHS == {
        "10m": benchmark.SERVER_ROOT / "assets" / "10m.mp3",
    }
    assert benchmark.CHUNK_SECONDS_OPTIONS == [120, 300]
    assert benchmark.CONCURRENCY_OPTIONS == [2, 4, 6, 8]
    assert len(results) == 8
    assert {result.file_label for result in results} == {"10m"}
    assert {result.chunk_seconds for result in results} == {120, 300}
    assert {result.concurrency for result in results} == {2, 4, 6, 8}
    assert all(result.status == "ok" for result in results)

    ok_result = next(
        result
        for result in results
        if result.file_label == "10m"
        and result.chunk_seconds == 120
        and result.concurrency == 2
    )
    expected_chunks = len(build_chunk_plan(600.0, 120, 2))
    assert ok_result.status == "ok"
    assert ok_result.chunk_count == expected_chunks
    assert ok_result.transcript_length > 0
    assert ok_result.estimated_cost_usd == 0.06
    assert len(chunking_service.calls) == 8


@pytest.mark.asyncio
async def test_single_benchmark_records_and_logs_rate_limit_failure(
    capsys,
    tmp_path,
) -> None:
    result = await benchmark.run_single_benchmark(
        file_label="10m",
        file_path=tmp_path / "10m.mp3",
        duration_seconds=600.0,
        chunk_seconds=120,
        concurrency=2,
        target_seconds=300,
        work_dir=tmp_path / "work",
        chunking_service=FakeChunkingService(),
        transcription_service=FakeTranscriptionService(concurrency=2, fail=True),
        target_chunk_max_mb=24,
        overlap_seconds=2,
    )

    assert result.status == "failed"
    assert result.rate_limit_error_count == 1
    assert result.failed_attempt_count == 1
    assert result.retry_count == 1
    assert result.error == "rate limit"
    output = capsys.readouterr().out
    assert "[10m] chunk=120s concurrency=2 시작..." in output
    assert "[10m] chunk=120s concurrency=2 →" in output
    assert "❌ rate_limit=1 retry=1" in output


@pytest.mark.asyncio
async def test_single_benchmark_logs_start_and_success(capsys, tmp_path) -> None:
    await benchmark.run_single_benchmark(
        file_label="10m",
        file_path=tmp_path / "10m.mp3",
        duration_seconds=600.0,
        chunk_seconds=120,
        concurrency=2,
        target_seconds=300,
        work_dir=tmp_path / "work",
        chunking_service=FakeChunkingService(),
        transcription_service=FakeTranscriptionService(concurrency=2),
        target_chunk_max_mb=24,
        overlap_seconds=2,
    )

    output = capsys.readouterr().out
    assert "[10m] chunk=120s concurrency=2 시작..." in output
    assert "[10m] chunk=120s concurrency=2 →" in output
    assert "✅ (within target)" in output


@pytest.mark.asyncio
async def test_benchmark_matrix_logs_skipped_cases(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(benchmark, "get_settings", fake_settings)

    await benchmark.run_benchmark_matrix(
        audio_file_paths={"10m": tmp_path / "10m.mp3"},
        chunk_seconds_options=[600],
        concurrency_options=[2],
        output_csv_path=tmp_path / "benchmark.csv",
        audio_analysis_service=FakeAudioAnalysisService({"10m.mp3": 600.0}),
        chunking_service=FakeChunkingService(),
        transcription_service_factory=lambda concurrency: FakeTranscriptionService(
            concurrency
        ),
        print_results=False,
    )

    output = capsys.readouterr().out
    assert "[10m] chunk=600s concurrency=2 → skipped" in output


def test_csv_and_console_table_include_required_columns(tmp_path) -> None:
    result = benchmark.BenchmarkResult(
        file_label="10m",
        file_path=tmp_path / "10m.mp3",
        audio_duration_seconds=600.0,
        chunk_seconds=120,
        concurrency=2,
        status="ok",
        total_seconds=12.34,
        chunk_count=5,
        chunk_create_seconds=1.2,
        stt_wall_seconds=11.14,
        rate_limit_error_count=0,
        failed_attempt_count=0,
        retry_count=0,
        transcript_length=1234,
        estimated_cost_usd=0.06,
        within_target=True,
        error="",
    )
    csv_path = tmp_path / "results.csv"

    benchmark.write_csv([result], csv_path)
    table = benchmark.render_table([result])

    with csv_path.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert rows[0].keys() == set(benchmark.RESULT_COLUMNS)
    assert rows[0]["file_label"] == "10m"
    assert rows[0]["chunk_count"] == "5"
    for column in benchmark.RESULT_COLUMNS:
        assert column in table


def test_rate_limit_detection_supports_exception_name_and_429_status() -> None:
    class RateLimitError(Exception):
        pass

    status_error = SimpleNamespace(status_code=429)

    assert benchmark.is_rate_limit_error(RateLimitError("slow down"))
    assert benchmark.is_rate_limit_error(status_error)
    assert not benchmark.is_rate_limit_error(RuntimeError("other error"))
