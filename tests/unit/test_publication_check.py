import base64
import gzip
import json
import runpy

from paper_review.config import ROOT

scanner = runpy.run_path(str(ROOT / "scripts/check_publication.py"))


def test_secret_check_does_not_echo_tokens_and_checks_encoded_fixture_content():
    token = "sk-" + "x" * 32
    assert scanner["scan"](token.encode()) == {"provider_token"}
    bundle = {"format": "recorded-regression-v1", "files": {
        "fixture": base64.b64encode(token.encode()).decode(),
    }}
    assert scanner["scan"](gzip.compress(json.dumps(bundle).encode()), compressed=True) == {
        "provider_token",
    }


def test_publication_paths_exclude_local_and_raw_run_data():
    for name in [".env", ".env.production", ".work/app.db", "paper.pdf", "run.log",
                 "frontend/node_modules/a.js", "evaluation/reports/raw.json"]:
        assert scanner["forbidden_path"](name)
    for name in ["uv.lock", "README.md", "src/app.py", "tests/fixtures/recorded-context.json.gz"]:
        assert not scanner["forbidden_path"](name)
