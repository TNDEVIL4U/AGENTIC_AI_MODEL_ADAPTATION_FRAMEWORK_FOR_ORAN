FROM python:3.11-slim AS app
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN pip install --no-cache-dir . \
 && useradd -m -u 10001 app && chown -R app /app
USER app
EXPOSE 8000
CMD ["sh", "-c", "python -c 'from oran_adapt.core.config import get_settings; from oran_adapt.db.migrate import upgrade_to_head; upgrade_to_head(get_settings().database_url)' && uvicorn oran_adapt.api.main:app --host 0.0.0.0 --port 8000"]
