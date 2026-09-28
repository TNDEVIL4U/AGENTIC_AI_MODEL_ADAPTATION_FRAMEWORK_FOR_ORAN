# MLflow tracking + registry server for docker-compose: run and registry records in PostgreSQL,
# model artifacts under /mlartifacts (a named volume in compose). The API talks only to this
# server (--serve-artifacts proxies artifact uploads/downloads), so it needs no storage access.
#
# mlflow and psycopg match requirements.lock.
FROM python:3.13.7-slim-bookworm
# An empty named volume mounted at /mlartifacts inherits the ownership set here.
RUN pip install --no-cache-dir --upgrade "pip>=26.2" \
 && pip install --no-cache-dir \
        mlflow==3.16.1 \
        "psycopg[binary]==3.3.6" \
 && groupadd --system --gid 10001 mlflow \
 && useradd --system --uid 10001 --gid mlflow --home-dir /tmp --no-create-home mlflow \
 && mkdir /mlartifacts && chown mlflow:mlflow /mlartifacts
ENV HOME=/tmp PYTHONUNBUFFERED=1 MLFLOW_DISABLE_AGENT_HINT=1
USER mlflow
EXPOSE 5000
