FROM python:3.12-slim

# ffmpeg is only needed by the optional Whisper path (audio download + decode),
# which is off by default. Uncomment it together with the `whisper` extra below.
# RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
#     && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app

# Installs the package (templates and static files travel with it) plus its deps.
RUN pip install --no-cache-dir . \
    # && pip install --no-cache-dir ".[whisper]" \
    && rm -rf /app/app /app/pyproject.toml \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app
VOLUME ["/app/data"]
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
