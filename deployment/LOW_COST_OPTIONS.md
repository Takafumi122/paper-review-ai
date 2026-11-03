# Lower-cost hosting proposal — 2026-11-01

The user requested a lower-cost alternative to the USD 25.25/month Render draft.
That draft remains unprovisioned and is on hold. No paid service, account, volume
or public URL was created during this comparison.

## Recommendation

For an initially small invitation group, prefer **Railway Hobby**, with one Docker
service and one persistent volume, API/Worker together, and no serverless sleeping.
Its USD 5/month minimum includes USD 5 of resource usage; usage above that is
extra, not a fixed-price USD 5 service. Set a Compute usage alert and a Hard limit
in the workspace dashboard to the owner's chosen budget. Reaching the Hard limit
takes workloads offline. These controls do not limit the app's external OpenAI bill.

The service should initially have a 1 GB RAM limit, one replica and a small volume
mounted at `/app/.work`; a hosted smoke test must confirm memory and volume access.
Use `python deployment/start_demo.py`, leave operator checks disabled and retain
both Lineage gates as false. The wrapper preserves S2 suspension. Avoid auto-sleep
until DB queue processing through idle periods has been validated.

Railway documents non-root volume permission issues. Before accepting any jobs,
verify that UID 10001 can write the mounted directory. A hosted permission strategy
has not been implemented or validated; do not silently run publicly as root or
claim this deployment is ready to share. The previous image has been locally
validated as non-root; changing its user needs a separately tested startup path.

Official sources: [pricing](https://railway.com/pricing),
[plan billing](https://docs.railway.com/pricing/plans),
[usage controls](https://docs.railway.com/pricing/cost-control),
[volume constraints](https://docs.railway.com/volumes/reference).

## Alternatives and costs

| Candidate | Infrastructure base cost | Decision |
| --- | --- | --- |
| Railway Hobby | USD 5/month minimum including usage, then additional usage | First candidate for low traffic; control spending with an explicit stop limit |
| Fly.io shared-cpu-1x, 1 GB RAM, 1 GB volume | USD 6.70 + 0.15 = 6.85 per 30 always-on days in the lowest-priced `iad`/`ewr` regions | Alternative when a specified VM size and clearer compute base are preferred |
| Render 512 MB, 1 GB disk | Approximately USD 7.25/month | Do not adopt unchanged: the synthetic 512 MB parser probe ran out of memory |
| Previous Render 2 GB draft | Approximately USD 25.25/month | On hold following user cost feedback |

All figures exclude OpenAI/API usage, tax, egress and other applicable charges.
Fly.io regional prices differ; USD 6.85 is not a Tokyo quotation. A 31-day month
costs more than the 30-day example. Avoid an extra Machine/replica, dedicated IPv4
or managed database for this single-host demo. No free-tier promise is made.
[Fly.io official pricing](https://docs.fly.io/about/pricing),
[Render official pricing](https://render.com/pricing).

At Railway's published per-second rates, continuous 0.25 GB RAM, 0.02 vCPU,
1 GB billed volume and 1 GB egress would be about USD 3.11 usage over 30 days,
so the USD 5 minimum would apply. At 0.75 GB RAM and 0.05 vCPU with the same
storage/traffic assumptions, it would be about USD 8.71. These are arithmetic
scenarios, not measurements or a prediction of this application's monthly bill.
Long-lived Worker memory retention, activity and platform measurement can change
the result. Reassess after observing actual hosted usage.

## Local capacity probe

A local ARM64 container ran the API and Worker with dummy credentials and no
analysis jobs. Under 512 MiB with swap disabled, HTTP preflight passed, but a
separate parser process reading a generated 50-page text-only PDF was OOM-killed
(exit 137, cgroup oom_kill=1). The same probe under 1 GiB completed, extracting
217,468 characters; cgroup peak was 611,647,488 bytes (about 583 MiB), with no OOM.
HTTP preflight passed afterwards; zero jobs and LLM calls were recorded.

This is a capacity probe, not actual-paper parsing validation, a worst-case bound,
or a complete Worker/LLM workload. In particular, the parser ran in a separate
process that exited, so sustained allocations in the production Worker were not
measured. Cloud architecture, original PDF layouts and runtime memory can differ.
The result supports trying 1 GB; it does not establish hosted readiness.

Recorded measurements: `evaluation/reports/low-cost-hosting-2026-11-01/memory-probe.json`.
No production code, model routing, ranking, canonical document, secret file,
commit, push or paid resource was changed by this comparison.
