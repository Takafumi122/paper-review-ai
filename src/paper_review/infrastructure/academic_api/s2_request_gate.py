"""Single-host, cross-process S2 send gate; no credentials or response bodies."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO

from paper_review.config import ROOT


class S2GateStopped(Exception):
    """An existing 429 circuit prevents sending."""


class SharedS2RequestGate:
    """Serialize sends across this workspace, including cooldown after completion.

    flock is held through the HTTP operation. Nonblocking acquisition never parks
    the event loop. Persistent wall-clock deadlines survive process restarts;
    clock rollback delays requests rather than granting extra capacity. A broken
    state file fails closed. All processes must share this local filesystem.
    """

    def __init__(
        self,
        path: Path = ROOT / ".work/s2-http-gate.json",
        *,
        stop_path: Path | None = ROOT / ".work/s2-rate-limited.json",
        interval: float = 1.05,
    ) -> None:
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("invalid_s2_interval")
        self.path = path
        self.stop_path = stop_path
        self.interval = interval

    @staticmethod
    def _write(handle: IO[str], state: dict) -> None:
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle)
        handle.flush()

    @asynccontextmanager
    async def request(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never unlink this file: replacing the inode would bypass existing locks.
        self.path.touch(exist_ok=True)
        with self.path.open("r+", encoding="utf-8") as handle:
            locked = False
            started = False
            state: dict = {}
            try:
                while not locked:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                handle.seek(0)
                raw = handle.read()
                state = json.loads(raw) if raw else {"not_before": time.time() + self.interval}
                deadline = state.get("not_before", 0)
                if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
                    raise ValueError("invalid_s2_gate_state")
                monotonic_deadline = time.monotonic() + max(0, deadline - time.time())
                while True:
                    if state.get("stopped") or (
                        self.stop_path is not None and self.stop_path.exists()
                    ):
                        raise S2GateStopped("semantic_scholar_circuit_open")
                    wait = max(deadline - time.time(), monotonic_deadline - time.monotonic())
                    if wait <= 0:
                        break
                    await asyncio.sleep(min(wait, 0.1))
                state["not_before"] = time.time() + self.interval
                self._write(handle, state)
                started = True
                yield handle
            finally:
                if locked:
                    try:
                        if started:
                            handle.seek(0)
                            state = json.load(handle)
                            state["not_before"] = time.time() + self.interval
                            self._write(handle, state)
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)

    def stop(self, handle: IO[str]) -> None:
        """Called under the send lock; all subsequent senders stop, even on restart."""
        handle.seek(0)
        state = json.load(handle)
        state["stopped"] = True
        self._write(handle, state)


def default_s2_request_gate() -> SharedS2RequestGate:
    return SharedS2RequestGate()
