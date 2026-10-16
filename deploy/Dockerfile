# syntax=docker/dockerfile:1
FROM node:22.19.0-bookworm-slim AS javascript
WORKDIR /build/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build
WORKDIR /build/agent-runner
COPY agent-runner/package*.json ./
RUN npm ci
COPY agent-runner/ ./
COPY skills/ /build/skills/
RUN npm run build && npm prune --omit=dev

# The pinned optional Docling stack requires Python 3.12 (including NumPy 2.5).
# The core application remains compatible with Python 3.11 outside this image.
FROM python:3.12.11-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NODE_ENV=production \
    PAPER_READER_RUNTIME_DIR=/runtime HF_HOME=/runtime/cache/huggingface \
    XDG_CACHE_HOME=/runtime/cache
RUN apt-get update && apt-get install --no-install-recommends -y \
    ca-certificates libcairo2 libglib2.0-0 libgl1 libgomp1 libseccomp2 tini poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
WORKDIR /app/backend
COPY backend/ ./
ARG INSTALL_DOCLING=true
RUN if [ "$INSTALL_DOCLING" = "true" ]; then pip install --no-cache-dir -c requirements-docling.lock '.[docling]'; \
    else pip install --no-cache-dir -c requirements.lock .; fi
COPY --from=javascript /build/web/dist /app/web/dist
COPY --from=javascript /build/agent-runner /app/agent-runner
COPY skills/ /app/skills/
RUN groupadd --gid 10001 paperreader && useradd --uid 10001 --gid 10001 --no-create-home paperreader
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
