import asyncio

from scripts import corpus_benchmark


def test_benchmark_runner_for_full_profile_returns_workflow_runner():
    runner = corpus_benchmark.benchmark_runner_for_profile(corpus_benchmark.BENCHMARK_PROFILE_FULL)
    assert runner is corpus_benchmark.benchmark_one_workflow


def test_benchmark_runner_for_assistive_core_profile_dispatches(monkeypatch):
    captured = {}

    async def _fake_runner(pdf_path, run_dir, settings, session_maker, job_manager):
        captured["args"] = (pdf_path, run_dir, settings, session_maker, job_manager)
        return "assistive-core"

    class _Settings:
        def model_copy(self, *, update):
            captured["update"] = update
            return f"settings:{update['skip_alt_text_generation']}"

    monkeypatch.setattr(corpus_benchmark, "benchmark_one_workflow", _fake_runner)
    runner = corpus_benchmark.benchmark_runner_for_profile(
        corpus_benchmark.BENCHMARK_PROFILE_ASSISTIVE_CORE
    )

    result = asyncio.run(runner("a.pdf", "out", _Settings(), "db", "jobs"))

    assert result == "assistive-core"
    assert captured["update"] == {"skip_alt_text_generation": True}
    assert captured["args"] == ("a.pdf", "out", "settings:True", "db", "jobs")


def test_benchmark_runner_for_deterministic_profile_dispatches(monkeypatch):
    captured = {}

    async def _fake_runner(pdf_path, run_dir, settings):
        captured["args"] = (pdf_path, run_dir, settings)
        return "deterministic"

    monkeypatch.setattr(corpus_benchmark, "benchmark_one", _fake_runner)
    runner = corpus_benchmark.benchmark_runner_for_profile(
        corpus_benchmark.BENCHMARK_PROFILE_DETERMINISTIC
    )

    result = asyncio.run(runner("a.pdf", "out", "settings", None, None))

    assert result == "deterministic"
    assert captured["args"] == ("a.pdf", "out", "settings")
