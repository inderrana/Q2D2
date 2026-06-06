# ---- builder stage: compile wheels with build-essential ----
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# ---- runtime stage: lean image, no build tools ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime-only deps: tesseract / ocrmypdf for OCR; curl for healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ocrmypdf tesseract-ocr tesseract-ocr-eng ghostscript \
        unpaper qpdf pngquant \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 1000 -m -d /home/appuser appuser \
    && mkdir -p /data /uploads \
    && chown -R appuser:appuser /data /uploads /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY --chown=appuser:appuser app ./app

USER appuser

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8088/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
