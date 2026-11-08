"""Check demo HTTP configuration without credentials or billable analysis requests."""

from __future__ import annotations

import argparse
import json
import re
from urllib.parse import urlsplit

import httpx


def verify(client: httpx.Client) -> dict[str, str | int]:
    health = client.get("/health")
    health.raise_for_status()
    data = health.json()
    if data.get("pipeline") != "full" or data.get("pipeline_version") != "full-v44":
        raise ValueError("unexpected_pipeline_version")
    if data.get("stage1_selection_policy_version") != "lower-bound-v1":
        raise ValueError("unexpected_stage1_selection_policy")
    if data.get("lineage_validation_version") != "edge-grounding-v3":
        raise ValueError("unexpected_lineage_validation_version")
    # Invalid input cannot enqueue work even if authorization is misconfigured.
    response = client.post("/analyses", json={"arxiv_url": "invalid-preflight-input"})
    if response.status_code != 401:
        raise ValueError("analysis_authentication_not_enforced")
    if client.get("/analyses/preflight-nonexistent-job").status_code != 401:
        raise ValueError("result_authentication_not_enforced")
    schema = client.get("/openapi.json")
    schema.raise_for_status()
    if any(path.startswith("/verification/") for path in schema.json().get("paths", {})):
        raise ValueError("operator_checks_enabled")
    page = client.get("/")
    page.raise_for_status()
    assets = sorted(set(re.findall(r'(?:src|href)="(?:\./|/)(assets/[^"?#]+)"', page.text)))
    if not assets:
        raise ValueError("built_frontend_missing")
    for asset in assets:
        response = client.get(f"/{asset}")
        response.raise_for_status()
        if "text/html" in response.headers.get("content-type", ""):
            raise ValueError("frontend_asset_returned_html")
    return {
        "status": "passed",
        "pipeline_version": data["pipeline_version"],
        "frontend_assets": len(assets),
        "analysis_requests_enqueued": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="HTTPS demo origin, or HTTP loopback for local verification")
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (
        url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
        or not url.hostname
        or not (
            url.scheme == "https"
            or (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})
        )
    ):
        parser.error("use a credential-free HTTPS origin or HTTP loopback origin")
    try:
        with httpx.Client(
            base_url=args.url.rstrip("/"), timeout=15, follow_redirects=False
        ) as client:
            result = verify(client)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # Never print response bodies, URLs with credentials or provider secrets.
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({"status": "failed", "reason": reason}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
