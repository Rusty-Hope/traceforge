# Multi-stage single-container build: React UI + FastAPI/ML backend on :8000
FROM node:26-alpine AS web-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY --from=web-build /build/frontend/dist frontend/dist/

RUN mkdir -p /data
VOLUME ["/data"]

ENV PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/traceforge.db

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
