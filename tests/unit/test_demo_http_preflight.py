"""Preflight fails closed and never submits valid work or operator calls."""

import json
import runpy
from pathlib import Path

import httpx
import pytest

VERIFY = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/verify_demo_http.py"))[
    "verify"
]


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        (None, None),
        ("analysis_auth", "analysis_authentication_not_enforced"),
        ("result_auth", "result_authentication_not_enforced"),
        ("operator", "operator_checks_enabled"),
        ("asset", "frontend_asset_returned_html"),
        ("version", "unexpected_pipeline_version"),
        ("policy", "unexpected_stage1_selection_policy"),
        ("merge", "unexpected_lineage_validation_version"),
    ],
)
def test_preflight_never_enqueues_valid_work(fault, reason):
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(
                200,
                json={
                    "pipeline": "full",
                    "pipeline_version": "old" if fault == "version" else "full-v35",
                    "stage1_selection_policy_version": (
                        "observed-renormalized-v1" if fault == "policy" else "lower-bound-v1"
                    ),
                    "lineage_validation_version": (
                        "old" if fault == "merge" else "merge-grounding-v2"
                    ),
                },
            )
        if path == "/analyses":
            assert request.method == "POST"
            assert json.loads(request.content) == {"arxiv_url": "invalid-preflight-input"}
            return httpx.Response(422 if fault == "analysis_auth" else 401)
        if path == "/analyses/preflight-nonexistent-job":
            return httpx.Response(404 if fault == "result_auth" else 401)
        if path == "/openapi.json":
            return httpx.Response(
                200, json={"paths": {"/verification/knowledge": {}} if fault == "operator" else {}}
            )
        if path == "/":
            return httpx.Response(200, text='<script src="./assets/main.js"></script>')
        assert path == "/assets/main.js" and request.method == "GET"
        return httpx.Response(
            200,
            text="<html>fallback</html>" if fault == "asset" else "export {};",
            headers={"content-type": "text/html" if fault == "asset" else "text/javascript"},
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://demo.test"
    ) as client:
        if reason:
            with pytest.raises(ValueError, match=reason):
                VERIFY(client)
        else:
            assert VERIFY(client)["status"] == "passed"
    assert all(not r.url.path.startswith("/verification/") for r in requests)
    assert all("authorization" not in r.headers for r in requests)
