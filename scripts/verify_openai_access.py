"""Operator-only metadata probe: no generation, no retries, no secret/raw output.

Run in the operator terminal with externally supplied OPENAI_API_KEY. This script
never opens .env; uv may load it externally when explicitly requested by the user.
"""

from __future__ import annotations

import json
import os

import httpx

from paper_review.llm.provider import ROUTING

SAFE_CODES = {
    "invalid_api_key",
    "model_not_found",
    "insufficient_permissions",
    "insufficient_quota",
}


def check_model(client: httpx.Client, model: str) -> dict:
    try:
        response = client.get(f"https://api.openai.com/v1/models/{model}")
    except httpx.HTTPError:
        return {"model": model, "status": None, "reason": "transport_error"}
    code = None
    if response.status_code != 200:
        try:
            candidate = response.json().get("error", {}).get("code")
            code = candidate if candidate in SAFE_CODES else "unclassified_provider_error"
        except (ValueError, AttributeError, TypeError):
            code = "unclassified_provider_error"
    return {"model": model, "status": response.status_code, "reason": code}


def main() -> None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print(json.dumps({"status": "missing_environment", "variable": "OPENAI_API_KEY"}))
        return
    with httpx.Client(
        headers={"Authorization": f"Bearer {key}"}, timeout=15, follow_redirects=False
    ) as client:
        for model in sorted({p.model for p in ROUTING.values()}):
            result = check_model(client, model)
            print(json.dumps(result), flush=True)
            if result["status"] in {None, 401, 403, 429}:
                break


if __name__ == "__main__":
    main()
