# Single-stage: the dependency set is small and the image stays ~200MB, so the
# complexity of a multi-stage build buys nothing here.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so application edits do not invalidate the install layer.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.32" \
      "pydantic>=2.9" \
      "pydantic-settings>=2.6" \
      "sqlalchemy[asyncio]>=2.0.36" \
      "asyncpg>=0.30" \
      "httpx>=0.27" \
      "pyyaml>=6.0" \
      "sse-starlette>=2.1"

COPY app ./app

# Runs unprivileged: nothing in this container needs root at runtime.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/corpus \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
