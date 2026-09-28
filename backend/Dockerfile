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
# The worker needs temporalio: the "workflows" extra of the same package.
RUN find src -name __pycache__ -type d -prune -exec rm -rf {} + \
    && pip install -e ".[workflows]"

COPY --from=db migrations /app/db/migrations
COPY --from=db backoffice_db /app/db/backoffice_db

USER backoffice:backoffice

EXPOSE 8000

# Behind a load balancer set FORWARDED_ALLOW_IPS so client addresses are trusted
# only from it. Worker: python -m backoffice.workflows.worker
# Migrations:            python -m backoffice_db migrate
CMD ["uvicorn", "backoffice.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
