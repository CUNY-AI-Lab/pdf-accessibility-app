from pathlib import Path

from app.pipeline.ocr import _build_ocrmypdf_args


def test_build_ocrmypdf_args_enables_rotate_and_deskew():
    args = _build_ocrmypdf_args(
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
    assert args[-2:] == ["input.pdf", "output.pdf"]


def test_build_ocrmypdf_args_respects_mode_and_optional_flags():
    args = _build_ocrmypdf_args(
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


def test_build_ocrmypdf_args_omits_deskew_in_redo_mode():
    args = _build_ocrmypdf_args(
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
