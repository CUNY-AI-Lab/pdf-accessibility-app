from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from app.config import Settings
from app.services import file_storage


@pytest.mark.asyncio
async def test_save_upload_rejects_empty_pdf(monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None,
        llm_base_url="http://localhost:11434/v1",
        upload_dir=tmp_path / "uploads",
        processing_dir=tmp_path / "processing",
        output_dir=tmp_path / "output",
    )
    monkeypatch.setattr(file_storage, "get_settings", lambda: settings)

    upload = UploadFile(filename="empty.pdf", file=BytesIO(b""))

    with pytest.raises(HTTPException) as exc_info:
        await file_storage.save_upload(upload)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "File is empty"
    assert not any(settings.upload_dir.glob("*"))


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
