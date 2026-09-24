# syntax=docker/dockerfile:1
# oran-adapt API / CDC consumer image. Multi-stage: dependencies are resolved in the builder,
# only the finished virtualenv and the source are copied into the runtime stage, which runs as
# an unprivileged user. CPU only (torch comes from the CPU wheel index; there is no GPU here).
#
#   docker build -t oran-adapt .
#
# Versions come from requirements.lock (scripts/lock_requirements.py). It is applied as a
# constraints file, so every locked package is installed at exactly its locked version, and the
# few Linux-only dependencies that a Windows-generated lock cannot contain are still resolved.

ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm

# ---- builder ---------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
# pip itself: 25.x has known advisories (pip-audit), fixed in 26.2.
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade "pip>=26.2"
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY requirements.lock pyproject.toml ./
COPY src ./src
# Editable install: the package stays at /app/src, where db/migrate.py finds alembic.ini and
# migrations/ (it resolves them relative to the source tree). The runtime stage keeps the same
# paths, so the install keeps working after the copy.
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu \
        -c requirements.lock --editable ".[kafka]"

# ---- runtime ---------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
# libgomp1: OpenMP runtime needed by xgboost's Linux wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip install --no-cache-dir --upgrade "pip>=26.2" \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MLFLOW_DISABLE_AGENT_HINT=1 \
    HOME=/tmp \
    ARTIFACT_WORKDIR=/tmp/oran-adapt/artifacts
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=root:root pyproject.toml alembic.ini ./
COPY --chown=root:root src ./src
COPY --chown=root:root migrations ./migrations
# Code is owned by root and read-only to the app user; only /tmp is writable.
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]
# Apply migrations, then serve. The CDC consumer service overrides this command.
CMD ["sh", "-c", "python -c 'from oran_adapt.core.config import get_settings; from oran_adapt.db.migrate import upgrade_to_head; upgrade_to_head(get_settings().database_url)' && exec uvicorn oran_adapt.api.main:app --host 0.0.0.0 --port 8000"]
