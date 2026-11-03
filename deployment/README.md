# Invite-only live demo deployment

Updated 2026-11-03 for the current full-v35 source. No public host or URL has been provisioned.
Earlier container measurements below describe full-v14; they are not a hosted full-v35 validation.
The later user request for lower cost puts the Render draft below on hold.
See [the lower-cost comparison](LOW_COST_OPTIONS.md) for the revised Railway
recommendation and measured 512 MB / 1 GB capacity limits.
This is a single-host Python API/Worker, React static build and persistent SQLite
deployment. The existing Dockerfile builds these together. Run one replica;
do not place SQLite on a shared network filesystem.

## Available behavior

Normal-path LoRA, Attention and ReAct runs generated and displayed Reasoning Flow
and prerequisite Knowledge under full-v14. Their new job costs total USD 0.422884
(estimated, not account billing). Each was one sample using cached parsed PDFs;
these runs do not establish new-download reliability or human quality acceptance.
See `evaluation/reports/reasoning-source-selection-v14-2026-11-01/`.

Research Lineage is blocked. Keep `PAPER_REVIEW_SPECTER2_VERIFIED=false` and
`PAPER_REVIEW_STAGE1_AUDITED=false`. Use `python deployment/start_demo.py` as the
demo start command. Before API/Worker startup it creates an operator-pause marker
on a fresh disk, using the existing S2 circuit interface. It does not invent a
429 response. Existing stop records are preserved byte-for-byte, without reading
or replacing them. A plain server start on a fresh volume would otherwise permit
S2 retrieval even though Lineage generation is blocked. Do not clear the marker
or enable gates as part of deployment. A new S2 verification window is separate work.

## Host configuration

Use a host with HTTPS termination, support for a continuously running container,
and a private persistent volume mounted at `/app/.work` (writable by UID 10001).
Keep the database and stop records on that volume across redeploys. Start with a
fresh demo database; the developer database contains evaluation history and is
not a public-demo seed. Do not copy `.env`, logs, evaluation databases or the
entire development `.work` directory into the image or demo volume.

Configure these values through the host's secret/environment settings, outside Git:

| Setting | Value or purpose |
| --- | --- |
| `OPENAI_API_KEY` | Provider key; never sent to browser |
| `SEMANTIC_SCHOLAR_API_KEY` | Provider key; existing S2 stop remains active |
| `PAPER_REVIEW_ACCESS_TOKEN` | Nonempty invitation code, shared only with demo users |
| `PAPER_REVIEW_DATABASE_PATH` | `/app/.work/app.db` |
| `PAPER_REVIEW_ENABLE_PROVIDER_CHECKS` | Unset; do not use `--operator-checks` publicly |
| `PAPER_REVIEW_SPECTER2_VERIFIED` | `false` |
| `PAPER_REVIEW_STAGE1_AUDITED` | `false` |
| `PAPER_REVIEW_MAX_QUEUED_JOBS` | Existing default 10; lower for a small invitation group if needed |
| `PAPER_REVIEW_MAX_DAILY_JOBS` | Existing default 25 accepted initial analyses per rolling 24 hours |

The default reservation guard is USD 1 per initial job and USD 0.10 per detail
job; up to 30 and 3 LLM HTTP attempts respectively. The initial-analysis daily
limit does not cap detail requests or total account spend. Invitation holders
share result access; this demo has no per-user data isolation. Configure the
provider account's spending controls for the intended invitation group.

## Build and start

From the repository, with Docker running:

```sh
docker build -t paper-review-ai:full-v35 .
```

Provision the persistent volume. Preserve an existing stop record if transferring
one; otherwise the demo start wrapper creates the explicit operator pause.
With the three secret variables already exported on the Docker host:

```sh
docker run --rm --name paper-review-demo \
  -p 127.0.0.1:8000:8000 \
  -e OPENAI_API_KEY -e SEMANTIC_SCHOLAR_API_KEY -e PAPER_REVIEW_ACCESS_TOKEN \
  -e PAPER_REVIEW_SPECTER2_VERIFIED=false -e PAPER_REVIEW_STAGE1_AUDITED=false \
  -v paper-review-data:/app/.work paper-review-ai:full-v35 \
  python deployment/start_demo.py
```

The image runs migrations and starts API/Worker on container port 8000. Terminate
HTTPS at the host/proxy and forward to the loopback port above. On a managed
container host, expose container port 8000 through its HTTPS service instead.
Never put provider keys into frontend build variables or command arguments.

## Verify before sharing

Run the non-billable HTTP preflight against the configured demo origin:

```sh
uv run --no-env-file python scripts/verify_demo_http.py https://YOUR-DEMO-HOST
```

It checks full-v35 / merge-grounding-v2, unauthenticated analysis/result rejection, absence of operator
verification routes, and built frontend asset delivery. It submits only an
invalid URL to test authorization, so even missing authentication cannot enqueue
an analysis. It does not use credentials, invoke operator endpoints, test TLS
configuration beyond the HTTPS client connection, or prove Worker/provider access.

After preflight, open the URL in a clean browser, enter the invitation code and
run one explicitly budgeted paper analysis. Confirm the persisted result after
reload, inspect its cost/status, and confirm Lineage's blocked state is clear.
This final hosted smoke test is not yet performed. Local full-v14 image build and
dummy-credential API/Worker startup passed after starting Docker. The container
ran as UID 10001, migrated to 0003, passed HTTP preflight and contained no `.env`;
its ledger had zero jobs and zero LLM calls. This did not test a hosted disk,
provider access from the container, or PDF parsing memory under load. The temporary
container was removed; the tested image is retained.

Git publication excludes `.env`, development databases, logs and ordinary operator
reports. See [the release status](../docs/release-status.md) for the current validation
scope. Review and deployment are separate from the historical container measurements.

## Selected first candidate: Render

Use the draft Blueprint at `deployment/render.yaml` (set this custom Blueprint
path during setup). It defines one Docker web service, Singapore region, a 2 GB
RAM / 1 CPU plan (`1c-2g`), a 1 GB disk at `/app/.work`, the demo startup wrapper,
manual deployments and secrets supplied through the dashboard. No service or paid
resource has been created. Automatic deploys are disabled, but creating a Blueprint
still performs its initial deployment: review the paid plan before that action.

As checked on 2026-11-01, the compute price is USD 25/month and disk storage is
USD 0.25/GB/month: approximately USD 25.25/month for this configuration before
API calls, tax and other metered charges. The 512 MB plan costs USD 7/month, but
PDF parsing plus API/Worker memory has not been shown to fit that limit. The 2 GB
choice is a conservative initial proposal, not a measured capacity guarantee.
Official sources: [pricing](https://render.com/pricing),
[compute plans](https://render.com/docs/compute-plans),
[Docker support](https://render.com/docs/docker), and
[Blueprint fields](https://render.com/docs/blueprint-spec).

Render's disk is restricted to one service instance and prevents zero-downtime
redeploys. This fits the current single-host design; expect a short interruption
on redeploy. Disks are not available at build/pre-deploy time, which is why pause
initialization and migrations happen at runtime.
[Disk constraints](https://render.com/docs/disks)

Railway is a viable alternative with a USD 5/month Hobby minimum including
USD 5 usage, then resource-based charges. Its documented non-root volume
permissions need attention with this image's UID 10001. Render is the first
candidate here because its selected compute size gives a clearer fixed base cost;
neither platform has been validated for this app on a hosted disk.
[Railway pricing](https://docs.railway.com/pricing/plans),
[Railway volume constraints](https://docs.railway.com/volumes/reference)
