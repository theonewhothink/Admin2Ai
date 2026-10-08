# syntax=docker/dockerfile:1.7
#
# Back-office backend image: the FastAPI api (default command), the Temporal
# worker and the database migration runner share it (§44).
#
# The build needs a second context holding db/ (migrations + backoffice_db):
#   docker build -f backend/Dockerfile --build-context db=db -t backoffice-backend backend
# docker compose passes it through build.additional_contexts.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/db

# psql is what the migration runner (python -m backoffice_db migrate) drives.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 backoffice \
    && useradd --system --uid 10001 --gid backoffice --home-dir /app --shell /usr/sbin/nologin backoffice

WORKDIR /app/backend

COPY pyproject.toml ./
COPY src ./src
# The worker needs temporalio (the "workflows" extra); the production API needs
# psycopg, boto3 and anthropic (the "server" extra), and reads uploaded PDFs and
# photos: "documents" (PDF text), "qr" (fiscal QR codes), "vision" (page rendering
# and masking before any external reading, §53), "ocr-local" (PP-OCRv6 in this
# process: photos and scanned PDFs are read with no sidecar and no network).
# RapidOCR depends on opencv-python, which needs X11/GL libraries a slim image
# does not have: the headless build of the same OpenCV version replaces it.
RUN find src -name __pycache__ -type d -prune -exec rm -rf {} + \
    && pip install -e ".[workflows,server,documents,qr,vision,ocr-local]" \
    && cv="$(pip show opencv-python 2>/dev/null | sed -n 's/^Version: //p')" \
    && if [ -n "$cv" ]; then pip uninstall -y opencv-python && pip install "opencv-python-headless==$cv"; fi \
    && python -c "from backoffice.ocr import local_ocr_available; assert local_ocr_available()"

COPY --from=db migrations /app/db/migrations
COPY --from=db backoffice_db /app/db/backoffice_db

# Evidence files when no S3 bucket is configured (BACKOFFICE_OBJECT_DIR); mount a
# volume here. Owned by the service user so a fresh named volume is writable.
RUN mkdir -p /var/lib/backoffice/objects \
    && chown -R backoffice:backoffice /var/lib/backoffice \
    && chmod 700 /var/lib/backoffice/objects

USER backoffice:backoffice

# Uvicorn reads these itself. WEB_CONCURRENCY: worker processes. In production
# mode tenants are shared through the database, so any number works (compose
# runs 2; on ECS scale tasks instead). The demo keeps its tenant in memory: 1.
# FORWARDED_ALLOW_IPS: the proxy allowed to set client addresses.
ENV WEB_CONCURRENCY=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1

EXPOSE 8000

# Behind a load balancer set FORWARDED_ALLOW_IPS to its address range so client
# addresses (rate limits) are trusted only from it.
# Worker:     python -m backoffice.workflows.worker
# Migrations: python -m backoffice_db migrate
CMD ["uvicorn", "backoffice.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--no-server-header", "--timeout-graceful-shutdown", "20"]
