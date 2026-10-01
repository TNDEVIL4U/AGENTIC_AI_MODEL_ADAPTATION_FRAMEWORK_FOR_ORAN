# syntax=docker/dockerfile:1
# oran-adapt images. One Dockerfile, three targets that share every layer but the last:
#
#   docker build --target api      -t oran-adapt-api .       # the REST API (uvicorn)
#   docker build --target worker   -t oran-adapt-worker .    # claims and runs adaptation jobs
#   docker build --target migrator -t oran-adapt-migrator .  # applies migrations, then exits
#
# The default target is `api`. Multi-stage: dependencies are resolved in the builder; only the
# finished virtualenv and the source reach the runtime stage, which runs as an unprivileged
# user with the code read-only. CPU only (torch comes from the CPU wheel index).
#
# Base image pinned by digest (the multi-arch index of python:3.13.7-slim-bookworm). To move
# it, update the tag and the digest together: docs/operations/images.md.
#
# Versions come from requirements.lock (scripts/lock_requirements.py), applied as a
# constraints file: every locked package is installed at exactly its locked version.
# The SBOM of each image is produced by the CI `images` job (docker buildx --sbom); the Python
# dependency SBOM by the `supply-chain` job.

ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d

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
# TORCH_INDEX_URL: the CPU wheel index; point it at a mirror for an air-gapped build.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --extra-index-url "${TORCH_INDEX_URL}" \
        -c requirements.lock --editable ".[kafka]"

# ---- runtime (shared by every target) --------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="oran-adapt" \
      org.opencontainers.image.version="${VERSION}"
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
# Code is owned by root and read-only to the app user; only /tmp is writable (in Kubernetes an
# emptyDir, so the root filesystem can be read-only).
USER 10001:10001

# ---- migrator: `oran-adapt db upgrade`, then exit (compose `migrate`, the Helm hook Job) -----
FROM runtime AS migrator
HEALTHCHECK NONE
CMD ["oran-adapt", "db", "upgrade"]

# ---- worker: claims jobs of its classes (JOB_WORKER_CLASSES) until SIGTERM drains it ---------
FROM runtime AS worker
ENV WORKER_HEALTH_FILE=/tmp/oran-adapt/worker.alive
# Liveness: the worker touches WORKER_HEALTH_FILE on every loop turn and job checkpoint.
HEALTHCHECK --interval=30s --timeout=20s --start-period=60s --retries=3 \
  CMD ["oran-adapt", "worker", "health"]
# SIGTERM drains: no new claims, the running job gets JOB_DRAIN_TIMEOUT_S, then is requeued.
STOPSIGNAL SIGTERM
CMD ["oran-adapt", "worker", "run"]

# ---- api (default): the REST API; the schema comes from the migrator, never from here -------
FROM runtime AS api
# uvicorn reads UVICORN_* for its options: on SIGTERM it stops accepting and gives in-flight
# requests this long.
ENV UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000 \
    UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=20
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD ["python", "-c", "import os,sys,urllib.request; url='http://127.0.0.1:%s/api/v1/health' % os.environ['UVICORN_PORT']; sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"]
STOPSIGNAL SIGTERM
CMD ["uvicorn", "oran_adapt.api.main:app"]
