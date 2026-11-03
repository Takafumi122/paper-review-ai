# Paper Review AI — project instructions

Help students understand LLM/NLP papers through Research Lineage, Reasoning Flow,
and Just-in-Time Background Knowledge. This is a paper-reading assistant, not an
accept/reject reviewer or absolute paper-quality scorer.

## Source of truth

Read relevant canonical documents and the latest evaluation reports before editing:

- `docs/requirements.md`
- `docs/architecture.md`
- `docs/research-lineage.md`
- `docs/scoring-lineage.md`
- `docs/llm-execution-spec.md`
- `docs/evaluation.md`
- `docs/thesis-choosing.md`
- `ADR/0001-mvp-decisions.md`

Follow `project-rules.md`. Do not modify canonical documents without an explicit
request. Report conflicts rather than changing requirements. Preserve missing
values, date precision, source/evidence provenance, and deterministic scoring.
Do not tune weights, aggregation, citation policy, tie-breaks or Top-K incidentally.

## Implementation and scope

- Prefer MVP/YAGNI, small reversible changes, and existing application/domain code.
- Python 3.12, uv, FastAPI, Pydantic, SQLAlchemy, Alembic, SQLite; typed Python.
- React, TypeScript, Vite for frontend work.
- Use deterministic processing; reserve LLMs for necessary semantic judgment.
- Do not add microservices, Redis, Celery, Kafka, vector DBs, Kubernetes, agent
  frameworks, ML runtimes, or unrelated abstractions without explicit justification.
- Inspect branch/status/diff first; preserve unrelated user changes. Do not use
  destructive Git cleanup, commit or push. Stay within the requested task and stop
  at its acceptance boundary; do not automatically start the next phase.
- Stage 2 stays blocked until its explicit prerequisites and ADR D1 are resolved.
  Synthetic vectors and successful offline tests do not establish live readiness.

## Security

Do not read/create/modify `.env`, inspect SSH files, acquire credentials, or store
secrets in code, reports, fixtures or logs. Do not print credentials. Only consume
externally supplied provider environment variables when the user explicitly
requests authenticated API work; test with dummy keys and mocked HTTP. This is
an authorized exception to the default no-credentials rule in `project-rules.md`.
Do not log paper bodies, raw prompts or raw provider responses. Keep writes and
validation databases/caches inside the workspace. Do not make live provider calls
unless authorized; respect request budgets and stop conditions.

## Validation

Run the requested checks after changes, report actual outcomes, and never weaken
or skip failing tests to claim completion:

```sh
uv sync
uv run pytest -q
uv run ruff check src tests scripts main.py migrations
uv run pyright
uv run alembic upgrade head
uv run alembic check
npm --prefix frontend test
npm --prefix frontend run build
```

Use `UV_CACHE_DIR=.work/uv-cache` and a separate workspace-local validation DB
through `PAPER_REVIEW_DATABASE_PATH` when appropriate. Do not overwrite historical
evaluation artifacts; write new reports to a new directory.
