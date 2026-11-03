# Railway Hobby — reading demo

Prepared 2026-11-03. This deploys new-URL Reasoning Flow and prerequisite Knowledge,
including requested details. Research Lineage remains blocked. No Railway resource
has been created by these instructions. API usage is billed separately.

## 1. Source and empty service

Create an Empty Project, then an Empty Service. Do not deploy the old `main`:
this guide requires `deployment/start_railway.py` and the reading-only changes.
Connect `Takafumi122/paper-review-ai` to the reviewed `codex/reading-web` branch,
or `main` only after that PR is merged. Use repository root `/` and the root
Dockerfile (auto-detected). No separate frontend or Worker service is required.
Keep automatic deployments disabled while configuring.

## 2. Persistent volume

Attach one volume at **`/app/.work`**. Start with 1 GB if the dashboard offers that
size. Keep one replica. Volumes are not mounted during build or pre-deploy, so
leave Pre-deploy Command empty; migrations run in the startup process.

Set **Start Command** to:

```text
python deployment/start_railway.py
```

Railway's documented `RAILWAY_RUN_UID=0` lets this initialization process fix only
the mount-root ownership and private mode (0700). It then clears supplementary
groups and drops permanently to UID/GID 10001 before migrations, API and Worker
start. It does not recursively chown data or keep the application running as root.
Use this command together with the UID variable, not the default Docker CMD.

## 3. Variables

Add provider and invitation secrets individually in Railway Variables; never
upload the local dotenv, developer DB or evaluation reports. Values below marked
secret must be supplied by the owner and must not be pasted into chat or Git.

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | Secret provider key |
| `PAPER_REVIEW_ACCESS_TOKEN` | Secret invitation code; choose a strong random value |
| `RAILWAY_RUN_UID` | `0` (initialization only; launcher drops to 10001) |
| `PORT` | `8000` |
| `PAPER_REVIEW_DATABASE_PATH` | `/app/.work/app.db` |
| `PAPER_REVIEW_READING_ONLY` | `true` |
| `PAPER_REVIEW_SPECTER2_VERIFIED` | `false` |
| `PAPER_REVIEW_STAGE1_AUDITED` | `false` |
| `PAPER_REVIEW_INITIAL_BUDGET_USD` | `0.50` |
| `PAPER_REVIEW_DETAIL_BUDGET_USD` | `0.10` |
| `PAPER_REVIEW_MAX_QUEUED_JOBS` | `2` |
| `PAPER_REVIEW_MAX_DAILY_JOBS` | `3` |

No S2 key is required. Leave `PAPER_REVIEW_ENABLE_PROVIDER_CHECKS` and
`VITE_RECORDED_PREVIEW` unset. Never use `--operator-checks` on the public service.
Daily initial-job limits do not include all detail costs; configure provider-side
spending controls as appropriate. Invitation holders share result access.

## 4. Deploy settings and HTTPS

- Replicas: 1. Start with a 1 GB memory ceiling. This is not reserved/billed RAM.
- Healthcheck Path: `/health`.
- Pre-deploy Command: empty.
- Serverless / App Sleep: disabled initially, to keep DB queue processing available.
- Apply staged changes and deploy the reviewed code.
- Settings > Networking / Public Networking > Generate Domain. Target port: 8000.
- Use the generated HTTPS `*.up.railway.app` address; no paid custom domain needed.

`/health` should show `pipeline=full`, `pipeline_version=full-v35`,
`reading_only=true`, initial budget 0.5 and both Lineage readiness flags false.
A public domain is not proof that the Worker can call the provider.

## 5. Verify before sharing

Send only the public HTTPS URL to the collaborator, not credentials. Run:

```sh
UV_CACHE_DIR=.work/uv-cache uv run --no-env-file python scripts/verify_demo_http.py https://YOUR-HOST.up.railway.app
```

This checks authorization, absence of operator routes, frontend assets and version,
without creating a job or invoking an LLM. Then manually confirm `reading_only=true`
in health, enter the invitation code in the browser and perform one separately
budgeted analysis. Check both reading features, quotes, and the intentionally
unavailable Lineage. Reload, redeploy, and confirm the saved result remains.
Do not count a cached/pre-recorded result as a fresh URL acceptance test.

## 6. Infrastructure costs

Hobby has a USD 5/month minimum including USD 5 usage, not USD 5 unlimited usage.
Memory, CPU, volume and outbound transfer can increase the bill. The owner's
JPY 1,000 infrastructure ceiling needs conversion/tax allowance at setup time.
In Workspace > Usage, set Compute Usage alerts and a Hard limit within the chosen
budget. Reaching that limit shuts down workloads, so continued availability is
conditional on remaining within it. Limits cover the workspace, including other
projects. Agent Usage controls do not cap this app's external OpenAI API calls.

## Validation performed locally

646 tests, Ruff, Pyright and image build passed. A root-owned 0700 tmpfs mounted
at `/app/.work`, startup as UID 0, dummy credentials and a 1 GiB limit reproduced
the ownership boundary: API and Worker both ran as UID 10001. HTTP preflight passed,
no jobs enqueued. A separate real DPO parsing probe peaked at ~324 MiB with idle
API/Worker; it does not establish full provider-load peak or hosted persistence.
No hosted provider/persistence/capacity check is claimed yet.

Official references:
[Docker deployment](https://docs.railway.com/services),
[start command](https://docs.railway.com/deployments/start-command),
[volumes and permissions](https://docs.railway.com/volumes),
[healthchecks](https://docs.railway.com/deployments/healthchecks),
[cost controls](https://docs.railway.com/pricing/cost-control).
