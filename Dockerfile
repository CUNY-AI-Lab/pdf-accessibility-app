FROM oven/bun:1.2.21 AS frontend-build

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

COPY backend/verapdf-auto-install.xml /tmp/verapdf-auto-install.xml
RUN wget -q -O /tmp/verapdf-installer.zip https://software.verapdf.org/releases/verapdf-installer.zip \
    && unzip -q /tmp/verapdf-installer.zip -d /tmp/verapdf-installer \
    && bash /tmp/verapdf-installer/verapdf-greenfield-1.28.2/verapdf-install /tmp/verapdf-auto-install.xml \
    && ln -sf /opt/verapdf/verapdf /usr/local/bin/verapdf \
    && rm -rf /tmp/verapdf-installer /tmp/verapdf-installer.zip /tmp/verapdf-auto-install.xml

COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --frozen --no-dev --no-install-project \
    && rm -rf /tmp/uv-cache

COPY backend/app /app/backend/app
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

RUN mkdir -p /app/data/uploads /app/data/processing /app/data/output /home/app/.cache \
    && chown -R app:app /app/data /home/app

ENV PATH="/app/backend/.venv/bin:${PATH}" \
    HOME=/home/app \
    XDG_CACHE_HOME=/home/app/.cache

USER app

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
    CMD wget -qO- http://127.0.0.1:8001/health || exit 1

ENTRYPOINT ["tini", "--"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
