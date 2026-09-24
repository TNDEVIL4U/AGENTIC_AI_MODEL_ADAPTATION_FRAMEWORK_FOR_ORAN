# MLflow tracking + registry server for docker-compose: run and registry records in PostgreSQL,
# model artifacts in MinIO (S3). The API talks only to this server (--serve-artifacts proxies
# artifact uploads/downloads), so it never needs S3 credentials itself.
#
# mlflow and psycopg match requirements.lock; boto3 is not in the lock (the app does not use it),
# so it is bounded to one major version instead.
FROM python:3.13.7-slim-bookworm
RUN pip install --no-cache-dir --upgrade "pip>=26.2" \
 && pip install --no-cache-dir \
        mlflow==3.16.1 \
        "psycopg[binary]==3.3.6" \
        "boto3>=1.35,<2" \
 && groupadd --system --gid 10001 mlflow \
 && useradd --system --uid 10001 --gid mlflow --home-dir /tmp --no-create-home mlflow
ENV HOME=/tmp PYTHONUNBUFFERED=1 MLFLOW_DISABLE_AGENT_HINT=1
USER mlflow
EXPOSE 5000
