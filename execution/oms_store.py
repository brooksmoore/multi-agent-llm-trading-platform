"""SQLite-backed append-only event log for the OMS.

Schema:
    oms_events(seq, ts, kind, order_id, payload_json)

Durability:
- WAL mode: fast concurrent reads while a writer is active.
- synchronous=NORMAL: sufficient for our recovery model. We flush after
  each append() (sqlite3 implicit commit) so a crash loses at most the
  in-flight transaction — and the broker is the source of truth, so any
  log/broker mismatch is caught by reconciliation.
- Append-only: we never UPDATE or DELETE rows. seq is monotonic.

Event payload encoding:
- All payloads are JSON dicts. Decimals are stored as strings (preserving
  precision); datetimes as ISO-8601 with explicit tz.
- Helpers `dumps()` / `loads()` handle Decimal/datetime/UUID/Enum.

The store has zero knowledge of event semantics — it only knows there are
named events with order_id and JSON payloads. Interpretation is the OMS's job.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from core.types import OrderId

# ─── Event kinds (string constants — append-only) ─────────────────────────────


class EventKind(StrEnum):
    """Every event the OMS may log. Add new kinds at the bottom; never rename."""

    ORDER_SUBMIT_INTENT = "order.submit_intent"
    ORDER_ACCEPTED = "order.accepted"
    ORDER_REJECTED = "order.rejected"
    FILL_RECEIVED = "fill.received"
    ORDER_CANCEL_REQUESTED = "order.cancel_requested"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_EXPIRED = "order.expired"
    RECONCILE_NOOP = "reconcile.noop"           # broker matched; recorded for audit
    RECONCILE_RECOVERED = "reconcile.recovered"  # we backfilled state from broker
    RECONCILE_ABANDONED = "reconcile.abandoned"  # broker has no record; gave up


@dataclass(frozen=True)
class LoggedEvent:
    """One row from the oms_events table."""

    seq: int
    ts: datetime
    kind: EventKind
    order_id: OrderId
    payload: dict[str, Any]


# ─── JSON serialization for Decimal/datetime/UUID/StrEnum ────────────────────

class _Encoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:  # noqa: ANN401
        if isinstance(o, Decimal):
            return {"__decimal__": str(o)}
        if isinstance(o, datetime):
            return {"__datetime__": o.isoformat()}
        if isinstance(o, uuid.UUID):
            return {"__uuid__": str(o)}
        if isinstance(o, StrEnum):
            return str(o)
        return super().default(o)


def _decode_hook(obj: dict[str, Any]) -> Any:  # noqa: ANN401
    if "__decimal__" in obj:
        return Decimal(obj["__decimal__"])
    if "__datetime__" in obj:
        return datetime.fromisoformat(obj["__datetime__"])
    if "__uuid__" in obj:
        return uuid.UUID(obj["__uuid__"])
    return obj


def dumps(obj: Any) -> str:  # noqa: ANN401
    return json.dumps(obj, cls=_Encoder)


def loads(s: str) -> Any:  # noqa: ANN401
    return json.loads(s, object_hook=_decode_hook)


# ─── Schema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oms_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    order_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oms_order_id ON oms_events(order_id);
CREATE INDEX IF NOT EXISTS idx_oms_kind ON oms_events(kind);
"""


# ─── OMSStore ─────────────────────────────────────────────────────────────────


class OMSStore:
    """SQLite WAL-backed append-only event log.

    Thread-safe via internal lock. Writes are committed eagerly so the
    log is durable on append() return.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        # check_same_thread=False — we serialize via our own lock so the
        # connection can be used from the broker callback thread.
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        # WAL + NORMAL is the recommended pairing for high write durability without sync overhead.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # ---- Writes ----

    def append(
        self,
        kind: EventKind,
        order_id: OrderId,
        payload: dict[str, Any],
        ts: datetime,
    ) -> int:
        """Append an event. Returns the assigned seq. Durable on return."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO oms_events (ts, kind, order_id, payload) VALUES (?, ?, ?, ?)",
                (ts.isoformat(), str(kind), str(order_id), dumps(payload)),
            )
            seq = cursor.lastrowid
            assert seq is not None  # AUTOINCREMENT always returns a rowid
            return seq

    # ---- Reads ----

    def iter_all(self) -> Iterator[LoggedEvent]:
        """Stream every event in seq order. Used for crash recovery."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT seq, ts, kind, order_id, payload FROM oms_events ORDER BY seq ASC"
            )
            for seq, ts, kind, order_id, payload in cursor:
                yield LoggedEvent(
                    seq=seq,
                    ts=datetime.fromisoformat(ts),
                    kind=EventKind(kind),
                    order_id=uuid.UUID(order_id),
                    payload=loads(payload),
                )

    def recent_by_kind(self, kind: EventKind, n: int) -> list[LoggedEvent]:
        """Return the *n* most recent events of a given kind, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, kind, order_id, payload FROM oms_events "
                "WHERE kind = ? ORDER BY seq DESC LIMIT ?",
                (str(kind), n),
            ).fetchall()
        return [
            LoggedEvent(
                seq=seq,
                ts=datetime.fromisoformat(ts),
                kind=EventKind(k),
                order_id=uuid.UUID(order_id),
                payload=loads(payload),
            )
            for seq, ts, k, order_id, payload in rows
        ]

    def iter_for_order(self, order_id: OrderId) -> Iterator[LoggedEvent]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT seq, ts, kind, order_id, payload FROM oms_events "
                "WHERE order_id = ? ORDER BY seq ASC",
                (str(order_id),),
            )
            rows = cursor.fetchall()
        for seq, ts, kind, oid, payload in rows:
            yield LoggedEvent(
                seq=seq,
                ts=datetime.fromisoformat(ts),
                kind=EventKind(kind),
                order_id=uuid.UUID(oid),
                payload=loads(payload),
            )

    def count(self) -> int:
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM oms_events")
            row = cursor.fetchone()
            return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> OMSStore:
        return self

    def __exit__(self, *args: Any) -> None:  # noqa: ANN401
        self.close()
