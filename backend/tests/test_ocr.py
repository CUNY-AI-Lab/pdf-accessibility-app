import json
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from app.config import Settings
from app.pipeline import ocr
from app.pipeline.ocr import build_ocrmypdf_args
from app.services import runtime_paths


def testbuild_ocrmypdf_args_enables_rotate_and_deskew():
    args = build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="skip",
        rotate_pages=True,
        deskew=True,
    )

    assert "--rotate-pages" in args
    assert "--deskew" in args
    assert "--skip-text" in args
    assert args[args.index("--jobs") + 1] == "1"
    assert args[args.index("--max-image-mpixels") + 1] == "75"
    assert args[-2:] == ["input.pdf", "output.pdf"]


def testbuild_ocrmypdf_args_respects_mode_and_optional_flags():
    args = build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="redo",
        rotate_pages=False,
        deskew=False,
    )

    assert "--rotate-pages" not in args
    assert "--deskew" not in args
    assert "--redo-ocr" in args
    assert "--skip-text" not in args


def testbuild_ocrmypdf_args_omits_deskew_in_redo_mode():
    args = build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="redo",
        rotate_pages=True,
        deskew=True,
    )

    assert "--rotate-pages" in args
    assert "--redo-ocr" in args
    assert "--deskew" not in args


def testbuild_ocrmypdf_args_accepts_resource_limits():
    args = build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="skip",
        rotate_pages=True,
        deskew=True,
        jobs=3,
        max_image_mpixels=42,
    )

    assert args[args.index("--jobs") + 1] == "3"
    assert args[args.index("--max-image-mpixels") + 1] == "42"


def test_enriched_subprocess_env_adds_common_binary_dirs(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(runtime_paths.Path, "exists", lambda self: str(self) == "/opt/homebrew/bin")
    env = runtime_paths.enriched_subprocess_env()

    assert "/opt/homebrew/bin" in env["PATH"]


def test_resolve_binary_prefers_explicit_path(tmp_path):
    binary = tmp_path / "tesseract"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    resolved = runtime_paths.resolve_binary("tesseract", explicit=str(binary))

    assert resolved == str(binary.resolve())


def test_enriched_subprocess_env_adds_configured_binary_dirs(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(binary_search_dirs="/srv/bin,/opt/tools/bin"),
    )

    env = runtime_paths.enriched_subprocess_env()

    assert "/srv/bin" in env["PATH"]
    assert "/opt/tools/bin" in env["PATH"]


def test_gateway_engine_loads_the_plugin_and_runs_pages_in_parallel():
    args = build_ocrmypdf_args(
        input_path=Path("input.pdf"),
        output_path=Path("output.pdf"),
        language="eng",
        mode="skip",
        rotate_pages=True,
        deskew=True,
        engine="gateway",
    )

    assert args[args.index("--plugin") + 1] == "app.pipeline.ocr_vlm_plugin"
    assert args[args.index("--jobs") + 1] == str(Settings().ocr_gateway_jobs)
    assert args[-2:] == ["input.pdf", "output.pdf"]


@pytest.mark.asyncio
async def test_gateway_engine_gives_ocrmypdf_the_connection_and_time_for_every_page(
    monkeypatch, tmp_path
):
    settings = Settings(
        llm_base_url="https://gateway.test/v1",
        llm_api_key="sk-app",
        llm_model="check-model",
        ocr_model="vision-model",
        ocr_gateway_jobs=8,
        ocr_gateway_page_seconds=300,
        subprocess_timeout_ocr=900,
    )
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    source = tmp_path / "in.pdf"
    scan = pikepdf.new()
    for _ in range(25):
        scan.add_blank_page()
    scan.save(source)
    seen = {}

    async def create_subprocess_exec(*args, env, **kwargs):
        seen.update(args=args, env=env)
        return SimpleNamespace(returncode=0)

    async def communicate(proc, timeout):
        seen["timeout"] = timeout
        return b"", b""

    monkeypatch.setattr(ocr.asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(ocr, "communicate_with_timeout", communicate)

    result = await ocr.run_ocr(source, tmp_path / "out.pdf", engine="gateway")

    assert result.success
    assert "--plugin" in seen["args"]
    assert seen["env"]["CAIL_OCR_BASE_URL"] == "https://gateway.test/v1"
    assert json.loads(seen["env"]["CAIL_OCR_HEADERS"]) == {"Authorization": "Bearer sk-app"}
    assert seen["env"]["CAIL_OCR_MODEL"] == "vision-model"
    assert seen["env"]["CAIL_OCR_PAGE_SECONDS"] == "300"
    # 25 pages at 8 at a time is 4 rounds, each within the page limit plus
    # time for a Tesseract fallback: more than the configured 900 s.
    assert seen["timeout"] == 4 * (300 + ocr.GATEWAY_PAGE_FALLBACK_SECONDS)


def test_build_ocrmypdf_args_limits_redo_ocr_to_the_given_pages():
    args = build_ocrmypdf_args(
        input_path=Path("in.pdf"),
        output_path=Path("out.pdf"),
        language="eng",
        mode="redo",
        rotate_pages=True,
        deskew=True,
        pages=[0, 2, 5],
    )

    assert "--redo-ocr" in args
    assert args[args.index("--pages") + 1] == "1,3,6"
    assert args[-2:] == ["in.pdf", "out.pdf"]


def _write_pdf(path, page_count):
    with pikepdf.new() as pdf:
        for _ in range(page_count):
            pdf.add_blank_page()
        pdf.save(path)


@pytest.fixture
def simulate_ocr(monkeypatch, tmp_path):
    source = tmp_path / "in.pdf"
    output = tmp_path / "out.pdf"
    _write_pdf(source, 3)
    calls = []

    def install(attempts):
        async def create_subprocess_exec(*args, **kwargs):
            if calls:
                assert not output.exists()
            calls.append(args)
            return SimpleNamespace(returncode=attempts[len(calls) - 1][0])

        async def communicate(proc, timeout):
            _, content = attempts[len(calls) - 1]
            if isinstance(content, int):
                _write_pdf(output, content)
            elif content is not None:
                output.write_bytes(content)
            return b"", b"The generated PDF is INVALID"

        monkeypatch.setattr(ocr.asyncio, "create_subprocess_exec", create_subprocess_exec)
        monkeypatch.setattr(ocr, "communicate_with_timeout", communicate)
        return source, output, calls

    return install


@pytest.mark.asyncio
async def test_exit_4_accepts_readable_output_and_records_warning(simulate_ocr, caplog):
    source, output, calls = simulate_ocr([(4, 3)])
    # --pages still produces a whole document, not just the selected pages.
    result = await ocr.run_ocr(source, output, language="spa", pages=[1])
    assert result.success
    assert result.output_path == output
    assert not result.skipped
    assert len(calls) == 1
    assert "code 4" in result.message
    assert "expected page count" in result.message
    assert result.message in caplog.text
    assert "-O0" not in calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("unusable", [None, b"not a PDF", 2])
@pytest.mark.parametrize("retry_code", [0, 4])
async def test_exit_4_retries_unusable_output_once(simulate_ocr, unusable, retry_code, caplog):
    source, output, calls = simulate_ocr([(4, unusable), (retry_code, 3)])
    result = await ocr.run_ocr(source, output, language="por", mode="redo", pages=[1])
    assert result.success
    assert result.output_path == output
    assert len(calls) == 2
    assert calls[1] == (*calls[0][:-2], "-O0", *calls[0][-2:])
    assert calls[1][calls[1].index("--output-type") + 1] == "pdf"
    assert calls[1][calls[1].index("--language") + 1] == "por"
    assert calls[1][calls[1].index("--pages") + 1] == "2"
    assert "--redo-ocr" in calls[1]
    assert "retrying once" in result.message
    assert "-O0" in result.message
    assert "retrying once" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("unusable", [None, b"not a PDF", 2])
@pytest.mark.parametrize("retry_code", [0, 4])
async def test_exit_4_fails_when_retry_output_is_unusable(simulate_ocr, unusable, retry_code):
    source, output, calls = simulate_ocr([(4, b"broken"), (retry_code, unusable)])
    result = await ocr.run_ocr(source, output)
    assert not result.success
    assert result.output_path == source
    assert len(calls) == 2
    assert f"after retry (exit {retry_code})" in result.message
    if unusable == 2:
        assert "2 pages; expected 3" in result.message
    else:
        assert "Cannot open" in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, mode, success",
    [
        (0, "skip", True),
        (6, "skip", True),
        (6, "redo", False),
        (3, "skip", False),
        (7, "skip", False),
    ],
)
async def test_other_exit_codes_do_not_retry(simulate_ocr, code, mode, success):
    source, output, calls = simulate_ocr([(code, 3)])
    result = await ocr.run_ocr(source, output, mode=mode)
    assert result.success is success
    assert len(calls) == 1
    assert result.skipped is (code == 6 and mode == "skip")
    if not success:
        assert f"OCR failed (exit {code})" in result.message
