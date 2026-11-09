FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --locked --no-dev
ARG LOCAL_EMBEDDINGS=false
# Opt-in CPU runtime, matching the measured Linux evaluation environment.
# Metadata-only deployments retain the default image without ML dependencies.
RUN case "$LOCAL_EMBEDDINGS" in \
    false) ;; \
    true) uv pip install --python /app/.venv/bin/python \
              --index-url https://download.pytorch.org/whl/cpu 'torch==2.15.0' \
          && uv pip install --python /app/.venv/bin/python \
              'adapters==1.3.0' 'transformers==4.57.6' 'numpy==2.5.4' ;; \
    *) exit 1 ;; \
    esac
COPY main.py alembic.ini ./
COPY migrations/ ./migrations/
COPY scripts/prepare_local_specter2.py ./scripts/
COPY --from=frontend /build/dist ./frontend/dist
COPY deployment/start_demo.py deployment/start_railway.py deployment/verify_railway_graph.py ./deployment/
RUN useradd --uid 10001 --create-home app && mkdir .work && chown app:app .work
USER app
ENV PATH="/app/.venv/bin:$PATH"
ENV PAPER_REVIEW_DATABASE_PATH=/app/.work/app.db
EXPOSE 8000
CMD ["python", "-m", "paper_review.server", "--host", "0.0.0.0"]
