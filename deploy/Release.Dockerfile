FROM node:24.19.0-bookworm-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df AS web
WORKDIR /build/web
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM golang:1.26.5-bookworm@sha256:53eeac89074db483fdf0ab3be1df32bf6e47562263d2d0d6baa7f26acb4957dd AS api
WORKDIR /build/api
COPY api/go.mod api/go.sum ./
RUN go mod download
COPY api/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/crackrag-api ./cmd/server

FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY ai-runtime/requirements-m1.lock /app/ai-runtime/requirements-m1.lock
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --retries 0 torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --retries 0 -r /app/ai-runtime/requirements-m1.lock && python -m pip check
COPY --from=api /out/crackrag-api /app/api-bin/crackrag-api
COPY --from=web /build/web/dist /app/web/dist
COPY ai-runtime/src/crackrag/ /app/ai-runtime/src/crackrag/
COPY ai-runtime/src/crackrag_m1/ /app/ai-runtime/src/crackrag_m1/
COPY ai-runtime/prompts/ /app/ai-runtime/prompts/
COPY api/ /app/api/
COPY proto/ /app/proto/
COPY web/src/ /app/web/src/
COPY web/scripts/ /app/web/scripts/
COPY web/public/ /app/web/public/
COPY web/package.json web/package-lock.json web/tsconfig.json web/vite.config.ts web/index.html /app/web/
COPY requirements.lock /app/requirements.lock
COPY config/ /app/config/
COPY migrations/ /app/migrations/
COPY scripts/release/ /app/scripts/release/
COPY scripts/release.sh scripts/release.ps1 /app/scripts/
COPY deploy/ /app/deploy/
RUN useradd -u 10001 -m crackrag && mkdir -p /data/blobs /models /state /release && chown -R 10001:10001 /data /models /state /release
ENV PYTHONPATH=/app/ai-runtime/src PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
RUN python /app/scripts/release/image.py manifest
USER 10001:10001
CMD ["/app/api-bin/crackrag-api"]
