"""Heartbeat writer — proves the main loop is still alive.

A daemon thread writes `{"ts": iso8601, "uptime_s": int}` to `logs/heartbeat.json`
every 30s. The KillSwitchEngine.check_heartbeat() is fed from the same tick so
that a stuck main loop trips HEARTBEAT_MISSED.

Atomicity: write to a temp file in the same directory, then os.replace() onto
the destination. This guarantees external readers (the dashboard) never see a
half-written file.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from execution.kill_switch import KillSwitchEngine

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECS: float = 30.0


class HeartbeatWriter:
    """Background thread that periodically writes a heartbeat file."""

    def __init__(
        self,
        path: Path,
        kill: KillSwitchEngine | None = None,
        interval_secs: float = HEARTBEAT_INTERVAL_SECS,
    ) -> None:
        self._path = path
        self._kill = kill
        self._interval = interval_secs
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None

    def start(self) -> None:
        """Start the heartbeat thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="heartbeat-writer"
        )
        self._thread.start()
        log.info("HeartbeatWriter: started (path=%s interval=%.0fs)", self._path, self._interval)

    def stop(self) -> None:
        """Signal the thread to stop and wait for it (max 5s)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def tick_once(self, now: datetime | None = None) -> None:
        """Write a single heartbeat. Useful for tests; production uses the loop."""
        ts = now if now is not None else datetime.now(UTC)
        uptime_s = int(time.monotonic() - self._started_at) if self._started_at else 0
        self._write_atomic({"ts": ts.isoformat(), "uptime_s": uptime_s})
        if self._kill is not None:
            self._kill.record_heartbeat(ts)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick_once()
            except Exception:
                log.exception("HeartbeatWriter: tick failed")
            self._stop_event.wait(self._interval)

    def _write_atomic(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self._path)
