FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY ai-runtime/requirements-m1.lock /app/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --retries 0 torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --retries 0 --index-url https://pypi.org/simple -r requirements.lock && python -m pip check
COPY ai-runtime/src/crackrag/ /app/ai-runtime/src/crackrag/
COPY ai-runtime/src/crackrag_m1/ /app/ai-runtime/src/crackrag_m1/
COPY ai-runtime/prompts/ /app/ai-runtime/prompts/
COPY api/internal/app/m2_catalog.json /app/api/internal/app/m2_catalog.json
COPY config/ /app/config/
RUN useradd -u 10001 -m crackrag
USER crackrag
ENV PYTHONPATH=/app/ai-runtime/src PYTHONUNBUFFERED=1 PYTHONUTF8=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
EXPOSE 50051
CMD ["python", "-m", "crackrag_m1.server"]
