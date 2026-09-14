# Single-stage: the dependency set is small and the image stays ~200MB, so the
# complexity of a multi-stage build buys nothing here.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install from pyproject.toml's dependency list directly, not a hand-copied
# duplicate of it — a prior version hardcoded the pip install list here and it
# silently drifted out of sync when nh3 was added to pyproject.toml (checkpoint
# 3), crash-looping the container with ModuleNotFoundError. `pip install .`
# reads the same dependencies array pytest/uv install from, so it cannot drift
# again. app/ is copied first so `pip install .` (which needs the package
# present for hatchling to build it) doesn't require a second install layer.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Runs unprivileged: nothing in this container needs root at runtime.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/corpus \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
