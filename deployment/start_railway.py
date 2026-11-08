"""Initialize Railway's volume root, then run API/Worker as the image's app user."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

APP_UID = 10001
APP_GID = 10001
MODEL_MANIFEST_SHA256 = "4a67a30e47e79ca41ed5e702dccfb7fd0fe0f6a4da42e44374d90f06c3339014"


def prepare_model(root: Path) -> None:
    """Opt-in public model preparation on the mounted volume, as the app user."""
    if os.environ.get("PAPER_REVIEW_PREPARE_LOCAL_SPECTER2") != "true":
        return
    from paper_review.infrastructure.local_specter2 import file_hash, verified_bundle

    manifest = root / ".work/models/specter2/manifest.json"
    if not manifest.exists():
        subprocess.run(
            [sys.executable, str(root / "scripts/prepare_local_specter2.py"),
             "--directory", str(manifest.parent),
             "--base-revision", "3447645e1def9117997203454fa4495937bfbd83",
             "--adapter-revision", "2081559630a80fc5851d8f798a05ba81e9468089",
             "--download"], check=True, timeout=240,
        )
    if file_hash(manifest) != MODEL_MANIFEST_SHA256:
        raise RuntimeError("deployment_model_manifest_mismatch")
    verified_bundle(manifest)


def prepare_volume(directory: Path) -> None:
    if directory.is_symlink():
        raise RuntimeError("volume_directory_must_not_be_a_symlink")
    directory.mkdir(exist_ok=True)
    if os.getuid() == 0:
        # Only the mount root: never recursively traverse user/result files.
        os.chown(directory, APP_UID, APP_GID)
        os.chmod(directory, stat.S_IRWXU)
        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)
    if os.getuid() != APP_UID or not os.access(directory, os.W_OK | os.X_OK):
        raise RuntimeError("app_user_cannot_write_volume")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    prepare_volume(root / ".work")
    prepare_model(root)
    if os.environ.get("PAPER_REVIEW_RELEASE_CANARY") == "true":
        subprocess.Popen([sys.executable, str(root / "deployment/verify_railway_graph.py")])
    # Preserve the reading-only default. Only an explicit false value enables
    # configured candidate retrieval; Graph readiness remains independently gated.
    if os.environ.get("PAPER_REVIEW_READING_ONLY", "true").lower() in {
        "false", "0", "no", "off",
    }:
        os.execv(
            sys.executable,
            [sys.executable, "-m", "paper_review.server", "--host", "0.0.0.0"],
        )
        return
    os.execv(sys.executable, [sys.executable, str(root / "deployment/start_demo.py")])


if __name__ == "__main__":
    main()
