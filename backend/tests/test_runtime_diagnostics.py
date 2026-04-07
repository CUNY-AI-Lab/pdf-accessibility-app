from types import SimpleNamespace

from app.services import runtime_diagnostics


def test_extract_env_value_reads_process_environment_assignment():
    ps_output = (
        "PID TT STAT TIME COMMAND\n"
        "28181 ?? S 0:00.00 python DOCLING_DEVICE=mps PATH=/usr/bin:/bin\n"
    )

    assert runtime_diagnostics._extract_env_value(ps_output, "DOCLING_DEVICE") == "mps"


def test_parse_listener_pid_reads_lsof_machine_output():
    lsof_output = "p28181\ncpython3.12\n"

    assert runtime_diagnostics._parse_listener_pid(lsof_output) == 28181


def test_collect_runtime_diagnostics_reports_local_docling_gpu_state(monkeypatch):
    settings = SimpleNamespace(
        llm_base_url="https://openrouter.ai/api/v1",
        llm_model="google/gemini-3-flash-preview",
        docling_serve_url="http://localhost:5001",
        docling_serve_ocr_engine="rapidocr",
    )

    monkeypatch.setattr(
        runtime_diagnostics,
        "_inspect_local_docling_serve",
        lambda _port: {
            "listener_found": True,
            "process_pid": 28181,
            "device": "mps",
            "gpu_libraries_loaded": True,
        },
    )

    report = runtime_diagnostics.collect_runtime_diagnostics(settings)

    assert report["llm"]["uses_remote_api_endpoint"] is True
    assert report["docling"]["configured"] is True
    assert report["docling"]["local"] is True
    assert report["docling"]["device"] == "mps"
    assert report["docling"]["gpu_libraries_loaded"] is True
    assert report["pipeline"]["structure_runtime"] == "docling-serve (mps)"
