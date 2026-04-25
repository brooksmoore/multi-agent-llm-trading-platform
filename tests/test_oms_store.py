"""Tests for execution.oms_store — the SQLite WAL append-only event log.

Covers:
- Schema creation
- Append + read round-trip preserves Decimal/datetime/UUID/StrEnum
- Multiple events for the same order_id are returned in seq order
- Cross-instance durability: write with one OMSStore, open another, read
- iter_for_order filters correctly
- WAL mode is actually enabled
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.types import AgentId, OrderSide, new_id
from execution.oms_store import EventKind, OMSStore, dumps, loads

# ─── Encoding round-trip ──────────────────────────────────────────────────────


class TestEncoding:

    def test_decimal_round_trip(self) -> None:
        d = Decimal("123.456789012345")
        assert loads(dumps({"price": d}))["price"] == d

    def test_datetime_round_trip_with_tz(self) -> None:
        ts = datetime(2026, 4, 24, 14, 30, tzinfo=UTC)
        assert loads(dumps({"ts": ts}))["ts"] == ts

    def test_uuid_round_trip(self) -> None:
        u = new_id()
        assert loads(dumps({"id": u}))["id"] == u

    def test_strenum_serialized_as_string(self) -> None:
        # StrEnum becomes its string value on encode; consumer re-wraps with the enum.
        encoded = dumps({"side": OrderSide.BUY, "agent": AgentId.HAIKU})
        decoded = loads(encoded)
        assert decoded["side"] == "buy"
        assert decoded["agent"] == "haiku"
        # Re-wrap is the consumer's job
        assert OrderSide(decoded["side"]) == OrderSide.BUY


# ─── Append + iter ────────────────────────────────────────────────────────────


class TestAppendAndIter:

    def test_append_returns_increasing_seq(self, tmp_path: Path) -> None:
        store = OMSStore(tmp_path / "oms.db")
        order_id = new_id()
        ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        seq1 = store.append(EventKind.ORDER_SUBMIT_INTENT, order_id, {"x": 1}, ts)
        seq2 = store.append(EventKind.ORDER_ACCEPTED, order_id, {"x": 2}, ts)
        assert seq2 > seq1

    def test_count(self, tmp_path: Path) -> None:
        store = OMSStore(tmp_path / "oms.db")
        ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        for _ in range(5):
            store.append(EventKind.RECONCILE_NOOP, new_id(), {}, ts)
        assert store.count() == 5

    def test_iter_all_returns_events_in_order(self, tmp_path: Path) -> None:
        store = OMSStore(tmp_path / "oms.db")
        order_a = new_id()
        order_b = new_id()
        ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        store.append(EventKind.ORDER_SUBMIT_INTENT, order_a, {"x": 1}, ts)
        store.append(EventKind.ORDER_SUBMIT_INTENT, order_b, {"x": 2}, ts)
        store.append(EventKind.ORDER_ACCEPTED, order_a, {"x": 3}, ts)
        events = list(store.iter_all())
        assert [e.payload["x"] for e in events] == [1, 2, 3]

    def test_iter_for_order_filters(self, tmp_path: Path) -> None:
        store = OMSStore(tmp_path / "oms.db")
        order_a = new_id()
        order_b = new_id()
        ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        store.append(EventKind.ORDER_SUBMIT_INTENT, order_a, {"x": "a1"}, ts)
        store.append(EventKind.ORDER_SUBMIT_INTENT, order_b, {"x": "b1"}, ts)
        store.append(EventKind.ORDER_ACCEPTED, order_a, {"x": "a2"}, ts)
        events_a = list(store.iter_for_order(order_a))
        assert [e.payload["x"] for e in events_a] == ["a1", "a2"]

    def test_payload_round_trips_with_complex_types(self, tmp_path: Path) -> None:
        store = OMSStore(tmp_path / "oms.db")
        order_id = new_id()
        fill_id = new_id()
        ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        payload = {
            "fill_id": fill_id,
            "qty": Decimal("3.14159"),
            "ts": ts,
            "side": OrderSide.SELL,
        }
        store.append(EventKind.FILL_RECEIVED, order_id, payload, ts)
        events = list(store.iter_all())
        assert events[0].payload["fill_id"] == fill_id
        assert events[0].payload["qty"] == Decimal("3.14159")
        assert events[0].payload["ts"] == ts
        assert events[0].payload["side"] == "sell"


# ─── Cross-instance durability ────────────────────────────────────────────────


class TestDurability:

    def test_data_survives_close_and_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        order_id = new_id()
        ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)

        store1 = OMSStore(db)
        store1.append(EventKind.ORDER_SUBMIT_INTENT, order_id, {"x": 42}, ts)
        store1.close()

        store2 = OMSStore(db)
        events = list(store2.iter_all())
        assert len(events) == 1
        assert events[0].order_id == order_id
        assert events[0].payload["x"] == 42

    def test_simulated_crash_no_data_loss(self, tmp_path: Path) -> None:
        """Simulate crash: store handle is dropped without explicit close()."""
        db = tmp_path / "oms.db"
        ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        order_id = new_id()

        store1 = OMSStore(db)
        store1.append(EventKind.ORDER_SUBMIT_INTENT, order_id, {"x": "before crash"}, ts)
        # Simulate crash: lose handle without closing
        del store1

        store2 = OMSStore(db)
        events = list(store2.iter_all())
        assert len(events) == 1
        assert events[0].payload["x"] == "before crash"


# ─── WAL mode ─────────────────────────────────────────────────────────────────


class TestWalMode:

    def test_wal_mode_actually_enabled(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        OMSStore(db)  # init, then re-open in plain sqlite3 to query PRAGMA
        with sqlite3.connect(str(db)) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"


# ─── Context manager ──────────────────────────────────────────────────────────


class TestContextManager:

    def test_enter_exit_closes(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        with OMSStore(db) as store:
            store.append(EventKind.RECONCILE_NOOP, new_id(), {}, ts)
            assert store.count() == 1
        # After exit, calling on the closed store should raise
        with pytest.raises(sqlite3.ProgrammingError):
            store.append(EventKind.RECONCILE_NOOP, new_id(), {}, ts)
