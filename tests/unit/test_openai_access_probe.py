import json
import runpy
from pathlib import Path

import httpx

check_model = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/verify_openai_access.py")
)["check_model"]


def test_metadata_probe_sanitizes_and_never_generates():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            404, json={"error": {"code": "model_not_found", "message": "dummy-secret raw response"}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = check_model(client, "gpt-5.6-luna")
    assert result == {"model": "gpt-5.6-luna", "status": 404, "reason": "model_not_found"}
    assert "dummy-secret" not in json.dumps(result)
    assert len(calls) == 1 and calls[0].method == "GET"
    assert calls[0].url.path == "/v1/models/gpt-5.6-luna"
