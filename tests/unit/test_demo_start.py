"""Demo startup preserves existing stop provenance and never invents a 429."""

import json
import runpy
from pathlib import Path

PAUSE = runpy.run_path(str(Path(__file__).resolve().parents[2] / "deployment/start_demo.py"))[
    "ensure_s2_pause"
]


def test_fresh_demo_volume_is_paused_without_claiming_a_provider_response(settings):
    directory = settings.database_path.parent / "fresh"
    PAUSE(directory)
    assert json.loads((directory / "s2-rate-limited.json").read_text()) == {
        "reason": "operator_pause_pending_live_verification"
    }


def test_existing_stop_record_is_untouched(settings):
    directory = settings.database_path.parent
    path = directory / "s2-rate-limited.json"
    original = b'{"status":429,"provenance":"existing-operator-record"}\n'
    path.write_bytes(original)
    PAUSE(directory)
    PAUSE(directory)
    assert path.read_bytes() == original
