FROM oven/bun:1.2.21 AS frontend-build

ARG VITE_APP_BASE_PATH=/
ENV VITE_APP_BASE_PATH=${VITE_APP_BASE_PATH}

WORKDIR /app/frontend

COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile

COPY frontend /app/frontend
RUN bun run build

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

WORKDIR /app/backend

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        default-jre-headless \
        fontconfig \
        fonts-dejavu-core \
        fonts-liberation \
        ghostscript \
        libgl1 \
        libglib2.0-0 \
        libspatialindex-dev \
        poppler-utils \
        qpdf \
        tesseract-ocr \
        tesseract-ocr-eng \
        tini \
        unpaper \
        unzip \
        wget \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app
RUN mkdir -p \
        /app/data/uploads \
        /app/data/processing \
        /app/data/output \
        /app/data/debug \
        /home/app/.cache/docling \
        /home/app/.cache/huggingface \
        /home/app/.cache/torch \
        /home/app/artifacts/docling \
    && chown -R app:app /app/data /home/app

COPY backend/verapdf-auto-install.xml /tmp/verapdf-auto-install.xml
RUN wget -q -O /tmp/verapdf-installer.zip https://software.verapdf.org/releases/verapdf-installer.zip \
    && unzip -q /tmp/verapdf-installer.zip -d /tmp/verapdf-installer \
    && bash /tmp/verapdf-installer/verapdf-greenfield-1.28.2/verapdf-install /tmp/verapdf-auto-install.xml \
    && ln -sf /opt/verapdf/verapdf /usr/local/bin/verapdf \
    && rm -rf /tmp/verapdf-installer /tmp/verapdf-installer.zip /tmp/verapdf-auto-install.xml

COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --frozen --no-dev --no-install-project \
    && rapidocr_models_dir="$(find /app/backend/.venv/lib -type d -path '*/site-packages/rapidocr/models' -print -quit)" \
    && test -n "$rapidocr_models_dir" \
    && chown -R app:app "$rapidocr_models_dir" \
    && rm -rf /tmp/uv-cache
RUN cat > /tmp/preload_docling_models.py <<'PY'
from pathlib import Path
import os

from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
from docling.utils.model_downloader import download_models

artifacts = Path(os.environ["DOCLING_ARTIFACTS_PATH"])
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
    progress=False,
)
RapidOcrModel.download_models(
    backend="onnxruntime",
    local_dir=artifacts / RapidOcrModel._model_repo_folder,
    force=False,
    progress=False,
)
print(f"Preloaded Docling artifacts into {artifacts}")
PY
RUN su app -s /bin/sh -c 'HOME=/home/app XDG_CACHE_HOME=/home/app/.cache HF_HOME=/home/app/.cache/huggingface TORCH_HOME=/home/app/.cache/torch DOCLING_CACHE_DIR=/home/app/.cache/docling DOCLING_ARTIFACTS_PATH=/home/app/artifacts/docling /app/backend/.venv/bin/python /tmp/preload_docling_models.py' \
    && rm -f /tmp/preload_docling_models.py

COPY backend/app /app/backend/app
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

RUN mkdir -p /app/data/uploads /app/data/processing /app/data/output /app/data/debug /home/app/.cache \
    && rm -rf /app/backend/debug \
    && ln -s /app/data/debug /app/backend/debug \
    && chown -R app:app /app/data /home/app

ENV PATH="/app/backend/.venv/bin:${PATH}" \
    HOME=/home/app \
    XDG_CACHE_HOME=/home/app/.cache \
    HF_HOME=/home/app/.cache/huggingface \
    TORCH_HOME=/home/app/.cache/torch \
    DOCLING_CACHE_DIR=/home/app/.cache/docling \
    DOCLING_ARTIFACTS_PATH=/home/app/artifacts/docling \
    DOCLING_DEBUG_OUTPUT_PATH=/app/data/debug

USER app

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
    CMD wget -qO- http://127.0.0.1:8001/health || exit 1

ENTRYPOINT ["tini", "--"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
