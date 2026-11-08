"""Initialize Railway's volume root, then run API/Worker as the image's app user."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

APP_UID = 10001
APP_GID = 10001


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
    os.execv(sys.executable, [sys.executable, str(root / "deployment/start_demo.py")])


if __name__ == "__main__":
    main()
