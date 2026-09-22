FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEOLOOK_HOST=0.0.0.0 \
    GEOLOOK_PORT=8765 \
    GEOLOOK_ENV_FILE=/data/.env \
    GEOLOOK_WORK_DIR=/data/work \
    GEOLOOK_JOBS_DIR=/data/jobs

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --shell /usr/sbin/nologin geolook \
    && mkdir -p /data/work /data/jobs \
    && chown -R geolook:geolook /data

COPY . .
RUN chown -R geolook:geolook /app

USER geolook
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.environ.get('GEOLOOK_PORT','8765'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).read()"

CMD ["sh", "-c", "python scripts/geo.py ui --host ${GEOLOOK_HOST:-0.0.0.0} --port ${GEOLOOK_PORT:-8765} --no-open"]
