"""Tests for ops/equity_snapshotter.py — per-agent equity attribution.

The snapshotter must drive `tracker.update_on_mark()` on every tick using
broker-derived marks, so per-agent sleeve equity reflects unrealized PnL on
held lots even when the agent's `dispatch_observation` hasn't run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.types import AgentId, AssetClass, Fill, OrderSide, new_id
from execution.agent_state_tracker import AgentStateTracker
from execution.broker import BrokerAccount, BrokerPosition
from execution.kill_switch import KillSwitchEngine
from execution.lots import LotLedger
from ops.equity_snapshotter import EquitySnapshotter


@dataclass
class _StubBroker:
    """Minimal Broker stand-in: returns whatever positions/account we set."""

    positions: list[BrokerPosition] = field(default_factory=list)
    account_equity: Decimal = Decimal("100000")

    def list_positions(self) -> list[BrokerPosition]:
        return list(self.positions)

    def get_account(self) -> BrokerAccount:
        return BrokerAccount(
            cash=Decimal("0"),
            equity=self.account_equity,
            buying_power=Decimal("0"),
            pattern_day_trader=False,
            daytrade_count=0,
        )


def _pos(symbol: str, qty: Decimal, entry: Decimal, current: Decimal) -> BrokerPosition:
    cls = AssetClass.CRYPTO if "USD" in symbol or "/" in symbol else AssetClass.EQUITY
    return BrokerPosition(
        symbol=symbol,
        qty=qty,
        avg_entry_price=entry,
        current_price=current,
        asset_class=cls,
    )


def _opus_lot(symbol: str, qty: Decimal, entry: Decimal) -> Fill:
    return Fill(
        id=new_id(),
        order_id=new_id(),
        agent_id=AgentId.OPUS,
        symbol=symbol,
        side=OrderSide.BUY,
        qty=qty,
        price=entry,
        timestamp=datetime(2026, 5, 4, 14, 0, tzinfo=UTC),
    )


def _build(tmp_path: Path) -> tuple[AgentStateTracker, LotLedger, _StubBroker, EquitySnapshotter]:
    ledger = LotLedger()
    tracker = AgentStateTracker(
        kill_switch=KillSwitchEngine(),
        lot_ledger=ledger,
        starting_equity=Decimal("30000"),
        db_path=str(tmp_path / "tracker.db"),
    )
    broker = _StubBroker()
    snap = EquitySnapshotter(
        db_path=tmp_path / "equity.db",
        agent_state_tracker=tracker,
        broker=broker,
        lot_ledger=ledger,
    )
    return tracker, ledger, broker, snap


def _read_last_row(db_path: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT haiku_equity, sonnet_equity, opus_equity, manager_equity "
            "FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
    finally:
        conn.close()
    return {
        "haiku": row[0],
        "sonnet": row[1],
        "opus": row[2],
        "manager": row[3],
    }


def test_tick_drives_equity_recompute_from_broker_marks(tmp_path: Path) -> None:
    """The bug: Opus had open lots but sleeve equity was frozen. Fix: snapshotter
    must call update_on_mark using broker-derived marks BEFORE reading state."""
    tracker, ledger, broker, snap = _build(tmp_path)

    # Opus opens GOOGL @ 385 — entry recorded in lots, no observation yet.
    fill = _opus_lot("GOOGL", Decimal("1"), Decimal("385"))
    ledger.open_lot(fill)
    tracker.update_on_fill(fill)

    # Broker shows GOOGL marked up to 400.
    broker.positions = [_pos("GOOGL", Decimal("1"), Decimal("385"), Decimal("400"))]

    snap.tick_once(now=datetime(2026, 5, 5, 20, 0, tzinfo=UTC))

    row = _read_last_row(tmp_path / "equity.db")
    # 30000 starting + 1 share × (400 - 385) = 30015
    assert Decimal(row["opus"]) == Decimal("30015")


def test_tick_normalizes_crypto_symbol_form(tmp_path: Path) -> None:
    """Lots may carry 'BTC/USD' while broker reports 'BTCUSD'. The mark dict is
    keyed by the canonical (slash-stripped) form; the unrealized lookup must
    find it for either lot symbol form."""
    tracker, ledger, broker, snap = _build(tmp_path)

    # Lot persisted with slashed form.
    fill = Fill(
        id=new_id(),
        order_id=new_id(),
        agent_id=AgentId.OPUS,
        symbol="BTC/USD",
        side=OrderSide.BUY,
        qty=Decimal("0.01"),
        price=Decimal("80000"),
        timestamp=datetime(2026, 5, 4, 14, 0, tzinfo=UTC),
    )
    ledger.open_lot(fill)
    tracker.update_on_fill(fill)

    # Broker reports unslashed form, marked up.
    broker.positions = [_pos("BTCUSD", Decimal("0.01"), Decimal("80000"), Decimal("90000"))]

    snap.tick_once(now=datetime(2026, 5, 5, 20, 0, tzinfo=UTC))

    row = _read_last_row(tmp_path / "equity.db")
    # 30000 + 0.01 × (90000 - 80000) = 30100
    assert Decimal(row["opus"]) == Decimal("30100")


def test_tick_does_not_hang_when_broker_blocks_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: the snapshotter wedged for ~24h when an alpaca-py call
    hung on a DNS-failed socket (alpaca-py exposes no request timeout).
    Watchdog must abort the call and let the loop continue with NULL NAV.
    """
    tracker, ledger, broker, snap = _build(tmp_path)

    # Shorten the watchdog timeout so the test takes ~0.2s, not 10s.
    monkeypatch.setattr(
        "ops.equity_snapshotter.BROKER_CALL_TIMEOUT_SECS", 0.2,
    )

    import threading
    block = threading.Event()  # never set → simulates an indefinite hang

    def _hang_account() -> BrokerAccount:
        block.wait()  # blocks until the test ends; daemon thread is leaked
        raise AssertionError("unreachable")

    def _hang_positions() -> list[BrokerPosition]:
        block.wait()
        raise AssertionError("unreachable")

    broker.get_account = _hang_account  # type: ignore[method-assign]
    broker.list_positions = _hang_positions  # type: ignore[method-assign]

    start = datetime.now(UTC)
    with caplog.at_level("WARNING", logger="ops.equity_snapshotter"):
        snap.tick_once(now=datetime(2026, 5, 5, 20, 0, tzinfo=UTC))
    elapsed = (datetime.now(UTC) - start).total_seconds()

    # Each call has its own 0.2s timeout; total tick should finish well under 1s.
    assert elapsed < 1.0, f"tick blocked for {elapsed:.2f}s — watchdog not firing"

    # The row is still written so the time series has a heartbeat — total_nav
    # is NULL because we couldn't read it, but sleeve equity (computed from
    # the ledger) is still meaningful.
    conn = sqlite3.connect(str(tmp_path / "equity.db"))
    try:
        cur = conn.execute("SELECT total_nav FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] is None  # total_nav NULL because account call hung

    # Surfaces the timeout as a WARNING so it's visible in logs, not silent.
    assert any(
        "get_account" in r.message and "exceeded" in r.message for r in caplog.records
    )

    # Unblock the leaked daemon threads so the test process exits cleanly.
    block.set()


def test_tick_with_no_broker_positions_keeps_starting_equity(tmp_path: Path) -> None:
    """Sanity: agents with no lots should remain at starting equity."""
    tracker, ledger, broker, snap = _build(tmp_path)
    broker.positions = []

    snap.tick_once(now=datetime(2026, 5, 5, 20, 0, tzinfo=UTC))

    row = _read_last_row(tmp_path / "equity.db")
    assert Decimal(row["opus"]) == Decimal("30000")
    assert Decimal(row["haiku"]) == Decimal("30000")
    assert Decimal(row["sonnet"]) == Decimal("30000")


def test_missing_mark_for_held_lot_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If a lot's symbol is absent from the mark dict, the unrealized PnL must
    log a WARNING — silently zeroing was the bug that hid the original issue."""
    tracker, ledger, broker, snap = _build(tmp_path)

    fill = _opus_lot("META", Decimal("0.5"), Decimal("600"))
    ledger.open_lot(fill)
    tracker.update_on_fill(fill)

    # Broker returns NO positions — no marks available.
    broker.positions = []

    with caplog.at_level("WARNING", logger="execution.agent_state_tracker"):
        snap.tick_once(now=datetime(2026, 5, 5, 20, 0, tzinfo=UTC))

    assert any(
        "missing marks for held lots" in rec.message and "META" in rec.message
        for rec in caplog.records
    )


def test_agent_position_snapshots_written_per_agent_symbol(tmp_path: Path) -> None:
    """Per-agent positions are persisted with attribution, qty-weighted entry,
    and current mark joined from broker prices."""
    tracker, ledger, broker, snap = _build(tmp_path)

    # Two Opus lots in GOOGL (qty-weighted entry should be 390).
    f1 = _opus_lot("GOOGL", Decimal("1"), Decimal("385"))
    f2 = _opus_lot("GOOGL", Decimal("1"), Decimal("395"))
    ledger.open_lot(f1)
    ledger.open_lot(f2)
    tracker.update_on_fill(f1)
    tracker.update_on_fill(f2)

    # Sonnet holds NVDA.
    sonnet_fill = Fill(
        id=new_id(), order_id=new_id(),
        agent_id=AgentId.SONNET,
        symbol="NVDA", side=OrderSide.BUY,
        qty=Decimal("10"), price=Decimal("200"),
        timestamp=datetime(2026, 5, 4, 14, 0, tzinfo=UTC),
    )
    ledger.open_lot(sonnet_fill)
    tracker.update_on_fill(sonnet_fill)

    broker.positions = [
        _pos("GOOGL", Decimal("2"), Decimal("390"), Decimal("400")),
        _pos("NVDA", Decimal("10"), Decimal("200"), Decimal("210")),
    ]

    snap.tick_once(now=datetime(2026, 5, 5, 20, 0, tzinfo=UTC))

    conn = sqlite3.connect(str(tmp_path / "equity.db"))
    try:
        rows = conn.execute(
            "SELECT agent_id, symbol, qty, avg_entry_price, mark_price, "
            "       market_value, unrealized_pl "
            "FROM agent_position_snapshots ORDER BY agent_id, symbol"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    by_agent = {(r[0], r[1]): r for r in rows}

    opus = by_agent[("opus", "GOOGL")]
    assert Decimal(opus[2]) == Decimal("2")
    assert Decimal(opus[3]) == Decimal("390")  # qty-weighted: (1*385 + 1*395) / 2
    assert Decimal(opus[4]) == Decimal("400")
    assert Decimal(opus[5]) == Decimal("800")  # 2 * 400
    assert Decimal(opus[6]) == Decimal("20")   # (400-390)*2

    sonnet = by_agent[("sonnet", "NVDA")]
    assert Decimal(sonnet[2]) == Decimal("10")
    assert Decimal(sonnet[6]) == Decimal("100")  # (210-200)*10


def test_agent_position_snapshot_null_mark_when_missing(tmp_path: Path) -> None:
    """If broker has no mark for a held symbol, the row is still recorded with
    NULL mark/value/unrealized — not silently dropped."""
    tracker, ledger, broker, snap = _build(tmp_path)

    fill = _opus_lot("META", Decimal("1"), Decimal("600"))
    ledger.open_lot(fill)
    tracker.update_on_fill(fill)

    broker.positions = []  # no marks

    snap.tick_once(now=datetime(2026, 5, 5, 20, 0, tzinfo=UTC))

    conn = sqlite3.connect(str(tmp_path / "equity.db"))
    try:
        rows = conn.execute(
            "SELECT mark_price, market_value, unrealized_pl "
            "FROM agent_position_snapshots WHERE agent_id='opus' AND symbol='META'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0] == (None, None, None)


def _count_equity_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0])
    finally:
        conn.close()


def test_identical_ticks_same_day_are_deduped(tmp_path: Path) -> None:
    """Frozen markets produce byte-identical snapshots every ~60s; only the
    first should be persisted. A value change writes a new row, and a new
    calendar day always anchors a row even when nothing moved."""
    _, _, broker, snap = _build(tmp_path)
    db = tmp_path / "equity.db"

    # Three identical ticks on the same day → one row.
    snap.tick_once(now=datetime(2026, 5, 4, 16, 0, tzinfo=UTC))
    snap.tick_once(now=datetime(2026, 5, 4, 16, 1, tzinfo=UTC))
    snap.tick_once(now=datetime(2026, 5, 4, 16, 2, tzinfo=UTC))
    assert _count_equity_rows(db) == 1

    # NAV changes → new row.
    broker.account_equity = Decimal("100500")
    snap.tick_once(now=datetime(2026, 5, 4, 16, 3, tzinfo=UTC))
    assert _count_equity_rows(db) == 2

    # Same values but a new calendar day → anchor row still written.
    snap.tick_once(now=datetime(2026, 5, 5, 13, 30, tzinfo=UTC))
    assert _count_equity_rows(db) == 3
