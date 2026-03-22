"""Preload Docling models at Docker build time.

This ensures models are baked into the image and don't need to be
downloaded at runtime, which dramatically reduces cold start time.
"""

from pathlib import Path

from docling.utils.model_downloader import download_models

# Use the default cache directory inside the container
artifacts = Path.home() / ".cache" / "docling" / "models"
artifacts.mkdir(parents=True, exist_ok=True)

download_models(
    output_dir=artifacts,
    with_layout=True,
    with_tableformer=True,
    with_code_formula=False,
    with_picture_classifier=True,
    with_smolvlm=False,
    with_granitedocling=False,
    with_granitedocling_mlx=False,
    with_smoldocling=False,
    with_smoldocling_mlx=False,
    with_granite_vision=False,
    with_granite_chart_extraction=False,
    with_rapidocr=False,
    with_easyocr=False,
    progress=True,
)

print(f"Preloaded Docling artifacts into {artifacts}")
