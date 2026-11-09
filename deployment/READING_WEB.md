# Reading-onlyの現在の位置づけ

full-v44でも候補取得・Graphを実行しない独立モードとして利用できます。既存の公開本番はOpenAlex＋CPU embedding構成へ移行しているため、現在の配置には[Railway手順](RAILWAY.md)を使ってください。以下の費用・VPS候補・テスト件数は2026-11-03時点の履歴で、現在の課金承認・ホスト選択を示しません。

---

# New-URL reading Web rollout — 2026-11-03

## Runtime

`--reading-only` runs the normal full-text parser, Reasoning Flow generation,
per-claim evidence review and prerequisite Knowledge generation. It skips S2
client initialization, candidate retrieval/ranking, citation classification and
Lineage generation. It needs no S2 key. The normal full mode is unchanged.
Lineage is explicitly blocked with `reading_only_mode`, including when both
readiness flags happen to be true. This is partial success, not complete Lineage.
The mode is exposed through `/health` and included in initial-job deduplication.
Existing scores, model routes, evidence validators and S2 stop records are preserved.

Supply the provider key externally through the environment. Do not load, copy,
print or publish dotenv files. From the repository root:

```sh
PAPER_REVIEW_DATABASE_PATH="$PWD/.work/reading-web.db" \
PAPER_REVIEW_INITIAL_BUDGET_USD=0.50 \
UV_CACHE_DIR=.work/uv-cache \
uv run --no-env-file python -m paper_review.server --reading-only --port 18080
```

This starts both API and Worker. The API key is passed by environment and is never
sent to the browser. An environment update does not change a running process. Stop this
launcher and start it again to reload API, Worker, code and environment together.
Codex can manage the process it starts; no terminal UI or key copying is needed.
Do not interrupt active provider calls to reload: first wait for a terminal job
state, preserving costs, reservations and checkpoints. Do not start a second
Worker against the same DB merely to reload. No file watcher automatically restarts
jobs in flight.

For hosted public binding, use `python deployment/start_demo.py` with a nonempty
`PAPER_REVIEW_ACCESS_TOKEN` supplied through the host's secret configuration.
Do not enable operator endpoints. Build the normal frontend, without
`VITE_RECORDED_PREVIEW=true`; the GitHub Pages preview remains independent.

## Budget and live acceptance

The previous evaluation ledger was USD 0.99639496 used/reserved against the
owner's USD 1.60 total ceiling. One new initial analysis may reserve up to USD
0.50 only after reconciling that ledger and verifying there are no concurrent
billable jobs. Knowledge detail requests are separately billed and not part of
that initial reservation; do not request them without checking the remaining
shared ceiling. Mocked tests do not spend API credit.

Use a supported arXiv paper that has no parsed revision or prior job in the
validation DB. Record URL/version, retrieval source/hash, job ID, mode, model
routes, feature statuses, token/cost totals and latency. Do not log bodies,
prompts or raw provider responses. Inspect actual selected quotes and generated
claims in the persisted result before calling the test passed. Preserve failures
and unknown-cost reservations; do not repeat submissions to obtain a better result.
A successful test needs both Reasoning and short Knowledge complete, sources
traceable, zero S2/candidate calls, and reloadable browser results. Report the
blocked Lineage separately. Fresh download and cached parsing are different checks.

## Constant-price hosting candidate

The owner chose an infrastructure ceiling of JPY 1,000/month, API usage separate.
As checked on 2026-11-03, Sakura VPS Ishikari 1 GB lists **JPY 880/month**, 2 virtual
cores and 50 GB SSD, initial fee zero. Use monthly billing, not an annual upfront
payment. Source: [official pricing](https://vps.sakura.ad.jp/).

This is a deployment candidate, not a purchased or validated host. Prior synthetic
parsing exceeded 512 MB; a historical 1 GB container probe succeeded, but no new
real-paper run on a 1 GB VPS has established headroom. Keep a single Worker and
one replica. If 1 GB is insufficient, stop and reassess rather than buy a larger
plan above the ceiling. No domain, paid backup or optional add-on is authorized
by this configuration. Confirm the total and contractual minimum term at checkout.

Before provisioning, prepare the existing container on a 1 GB memory limit with
a persistent `.work` volume, invitation authentication, HTTPS termination, and
operator routes disabled. Then confirm host volume permissions, restart recovery,
unauthenticated rejection and a separately budgeted hosted smoke analysis.
The server needs an owner-controlled hosting account and a hostname/HTTPS plan;
no account or server has been created. Do not upload the developer DB or `.env`.

## Local verification completed

2026-11-03: fresh DPO v3 URL, 27 pages, 8 Reasoning steps, 2 short Knowledge
items and one detail completed. New estimated API cost USD 0.150082. API/Worker
restart and browser result restoration passed. Offline checks: 643 pytest, Ruff,
Pyright, migrations, frontend 6 tests and build passed. No hosted deployment yet.
A local 1 GiB container parsed this real PDF while API/Worker were idle, peak
340,140,032 bytes; this is not full LLM-load or hosted peak memory. Detailed local
report: `evaluation/reports/reading-web-fresh-dpo-2026-11-03/RESULTS.md` (ignored).

The owner asked to compare Cloudflare, Railway and GCP before choosing a host.
No Sakura account, contract, paid resource or public live endpoint was created.
