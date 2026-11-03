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
COPY main.py alembic.ini ./
COPY migrations/ ./migrations/
COPY --from=frontend /build/dist ./frontend/dist
COPY deployment/start_demo.py ./deployment/start_demo.py
RUN useradd --uid 10001 --create-home app && mkdir .work && chown app:app .work
USER app
ENV PATH="/app/.venv/bin:$PATH"
ENV PAPER_REVIEW_DATABASE_PATH=/app/.work/app.db
EXPOSE 8000
CMD ["python", "-m", "paper_review.server", "--host", "0.0.0.0"]
