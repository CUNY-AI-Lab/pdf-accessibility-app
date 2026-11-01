from app.config import Settings
from app.services import file_storage


def test_get_output_path_sanitizes_nested_filename(monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None,
        llm_base_url="http://localhost:11434/v1",
        upload_dir=tmp_path / "uploads",
        processing_dir=tmp_path / "processing",
        output_dir=tmp_path / "output",
    )
    monkeypatch.setattr(file_storage, "get_settings", lambda: settings)

    output_path = file_storage.get_output_path(
        "job-1",
        "accessible_../../../../etc/passwd.pdf",
    )

    expected_dir = (settings.output_dir / "job-1").resolve()
    assert output_path.resolve().parent == expected_dir
    assert output_path.name == "passwd.pdf"
