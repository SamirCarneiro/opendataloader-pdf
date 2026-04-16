# syntax=docker/dockerfile:1.7
#
# Container image for the opendataloader-pdf hybrid server, targeted at
# Google Cloud Run.
#
# Build context: repository root.
#   docker build -f deploy/cloudrun/Dockerfile -t opendataloader-hybrid:latest .
#
# The image bundles:
#   - Docling (EasyOCR + TableFormer) for local deterministic enrichment
#   - google-genai SDK for Gemini 3.1 Flash picture descriptions
#   - FastAPI / uvicorn hybrid server (hybrid_server.py)
#
# Note: this image does NOT install the `opendataloader-pdf` Python wrapper
# package, because its custom hatch build hook requires the Java CLI JAR.
# The hybrid server is self-contained Python and does not need the JAR.
#
# Gemini is enabled at runtime via the OPENDATALOADER_USE_GEMINI=true env var
# (see service.yaml).

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Cache Docling / HuggingFace models in a writable, ephemeral location.
    # Pre-warming happens during `docker build` via the model download step.
    HF_HOME=/opt/models/hf \
    TORCH_HOME=/opt/models/torch \
    EASYOCR_MODULE_PATH=/opt/models/easyocr \
    DOCLING_ARTIFACTS_PATH=/opt/models/docling \
    # Cloud Run sets $PORT; hybrid_server reads it via DEFAULT_PORT.
    PORT=8080 \
    # Make the hybrid server package importable without a pip install.
    PYTHONPATH=/app

# System packages: libGL/libglib are required by EasyOCR + torchvision on slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install runtime dependencies directly. We mirror the `cloudrun` extra in
# pyproject.toml but avoid invoking hatch_build.py (which needs the Java JAR).
RUN pip install --upgrade pip && pip install \
    "docling[easyocr]>=2.0.0" \
    "fastapi>=0.100.0" \
    "uvicorn[standard]>=0.20.0" \
    "python-multipart>=0.0.22" \
    "google-genai>=0.3.0"

# Copy only the Python source needed for the hybrid server. The wrapper,
# generated CLI options and JAR bundling are irrelevant on the server side,
# so we also write a minimal __init__.py that doesn't pull in wrapper.py.
RUN mkdir -p /app/opendataloader_pdf && : > /app/opendataloader_pdf/__init__.py
COPY python/opendataloader-pdf/src/opendataloader_pdf/hybrid_server.py /app/opendataloader_pdf/hybrid_server.py
COPY python/opendataloader-pdf/src/opendataloader_pdf/gemini_enricher.py /app/opendataloader_pdf/gemini_enricher.py

# Pre-download Docling models so the first request on a cold Cloud Run
# instance doesn't pay the 30-60s model-download penalty.
RUN mkdir -p /opt/models \
    && python -c "from docling.utils.model_downloader import download_models; download_models()" \
    || echo "docling model pre-download skipped (non-fatal; models will download on first use)"

EXPOSE 8080

# tini reaps uvicorn subprocesses cleanly when Cloud Run sends SIGTERM.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Defaults are tuned for a Cloud Run instance backed by Gemini:
#   * No local picture-description model (SmolVLM is heavy; Gemini replaces it).
#   * Formula enrichment on (lightweight CPU model).
#   * force-ocr off (auto-detect).
# Override by setting `EXTRA_ARGS` on the Cloud Run service or editing service.yaml.
CMD ["sh", "-c", "python -m opendataloader_pdf.hybrid_server --host 0.0.0.0 --port ${PORT:-8080} --enrich-formula ${EXTRA_ARGS:-}"]
