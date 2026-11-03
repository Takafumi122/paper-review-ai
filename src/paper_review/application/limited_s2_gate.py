"""Explicit one-window recovery; ordinary clients always retain their stopped circuit."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from paper_review.infrastructure.academic_api.s2_request_gate import (
    S2GateStopped,
    SharedS2RequestGate,
)


class LimitedS2Gate(SharedS2RequestGate):
    """Temporarily permit one sender under the SAME inode/lock, re-stop before unlock.

    Only the frozen prior stop may be reviewed. A fresh 429, exception, unexpected
    state change or third request closes this window. The parent runner durably
    reserves the window before constructing this gate; it never resumes it.
    """

    def __init__(self, path: Path, *, expected_sha256: str, archive: Path,
                 window_id: str, interval: float = 1.05):
        super().__init__(path, stop_path=None, interval=interval)
        self.expected_sha256 = expected_sha256
        self.archive = archive
        self.window_id = window_id
        self.sent = 0
        self.failed = False

    def stop(self, handle) -> None:
        self.failed = True
        super().stop(handle)

    @asynccontextmanager
    async def request(self):
        # Missing file is an error, never permission to create a new lock inode.
        with self.path.open("r+", encoding="utf-8") as handle:
            locked = False
            started = False
            state = {}
            try:
                while not locked:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                raw = handle.read()
                if self.failed or self.sent >= 2:
                    raise S2GateStopped("limited_window_closed")
                if hashlib.sha256(raw.encode()).hexdigest() != self.expected_sha256:
                    raise S2GateStopped("limited_window_state_changed")
                state = json.loads(raw)
                if state.get("stopped") is not True:
                    raise S2GateStopped("reviewed_stopped_state_required")
                deadline = state.get("not_before")
                if (type(deadline) not in {int, float} or not math.isfinite(deadline)):
                    raise ValueError("invalid_s2_gate_state")
                if not self.sent:
                    with self.archive.open("xb") as saved:
                        saved.write(raw.encode())
                    deadline = max(deadline, time.time() + self.interval)
                monotonic_deadline = time.monotonic() + max(0, deadline-time.time())
                while True:
                    wait = max(deadline-time.time(), monotonic_deadline-time.monotonic())
                    if wait <= 0:
                        break
                    await asyncio.sleep(min(wait, 0.1))
                self.sent += 1
                state.update(stopped=False, recovery_window=self.window_id,
                             recovery_attempts=self.sent)
                self._write(handle, state)
                started = True
                try:
                    yield handle
                except BaseException:
                    self.failed = True
                    raise
            finally:
                if locked:
                    try:
                        if started:
                            # Never globally reopen the circuit, even after 200.
                            state.update(stopped=True, recovery_failed=self.failed,
                                         not_before=time.time() + self.interval)
                            self._write(handle, state)
                            handle.seek(0)
                            self.expected_sha256 = hashlib.sha256(
                                handle.read().encode()
                            ).hexdigest()
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)
