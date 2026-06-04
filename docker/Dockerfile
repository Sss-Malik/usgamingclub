FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir ".[prod]"

# API container overrides this; worker container sets its own command in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
