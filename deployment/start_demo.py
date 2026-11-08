"""Start an invite-only demo with academic requests paused pending verification."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def ensure_s2_pause(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with (directory / "s2-rate-limited.json").open("x") as marker:
            json.dump({"reason": "operator_pause_pending_live_verification"}, marker)
    except FileExistsError:
        # Keep the existing operator/circuit record without reading or replacing it.
        pass


def main() -> None:
    ensure_s2_pause(Path(__file__).resolve().parents[1] / ".work")
    os.execv(
        sys.executable,
        [sys.executable, "-m", "paper_review.server", "--reading-only", "--host", "0.0.0.0"],
    )


if __name__ == "__main__":
    main()
