# Multi-stage build for the FastAPI backend.
# Runs as non-root.
#
# Build context: the REPO ROOT (deploy.sh sets it to "$ROOT"). All COPY
# paths below are relative to that. We use root because we also need
# `ingestion/` which lives outside `backend/`, and Docker COPY doesn't
# allow `..`.
FROM python:3.12-slim AS deps
WORKDIR /build
COPY backend/requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
RUN useradd -u 10001 -m -s /usr/sbin/nologin app
WORKDIR /app
COPY --from=deps /install /usr/local
COPY backend/app /app/app
COPY ingestion   /app/ingestion
USER app
EXPOSE 8000
ENV PYTHONPATH=/app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
