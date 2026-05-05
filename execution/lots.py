"""Tax lot ledger with FIFO and LIFO consumption.

Open one lot per BUY fill; consume lots (FIFO or LIFO) for each SELL fill.
Thread-safe; all mutations hold a Lock.

Optionally persists to SQLite (one row per lot + a booked_fills table for
idempotency). On construction, reloads any existing lots so state survives
restarts. Without persistence the ledger is in-memory only.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

from core.types import AgentId, Fill, FillId, Lot, LotId, LotMethod, OrderSide, new_id


def _to_uuid(v: object) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


_DDL_LOTS = """
CREATE TABLE IF NOT EXISTS lots (
  id              TEXT PRIMARY KEY,
  agent_id        TEXT NOT NULL,
  symbol          TEXT NOT NULL,
  qty             TEXT NOT NULL,
  entry_price     TEXT NOT NULL,
  entry_date      TEXT NOT NULL,
  entry_fill_id   TEXT NOT NULL UNIQUE,
  remaining_qty   TEXT NOT NULL,
  exit_fill_id    TEXT,
  exit_date       TEXT,
  exit_price      TEXT,
  is_closed       INTEGER NOT NULL DEFAULT 0
)
"""

_DDL_LOTS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_lots_agent_symbol "
    "ON lots(agent_id, symbol, is_closed)"
)

# Idempotency: every fill we book is recorded here so book_fill() is safe to
# call on duplicate FillReceivedEvents (e.g. reconciler re-emitting on restart).
_DDL_BOOKED = """
CREATE TABLE IF NOT EXISTS booked_fills (
  fill_id TEXT PRIMARY KEY,
  side    TEXT NOT NULL
)
"""


class LotLedger:
    """In-memory ledger of all tax lots across all agents, optionally persisted."""

    def __init__(self, db_path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._lots: dict[LotId, Lot] = {}
        self._booked: set[FillId] = set()
        self._db_path = db_path
        if self._db_path is not None:
            self._init_db()
            self._load_from_db()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        assert self._db_path is not None
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(_DDL_LOTS)
            conn.execute(_DDL_LOTS_IDX)
            conn.execute(_DDL_BOOKED)
            conn.commit()
        finally:
            conn.close()

    def _load_from_db(self) -> None:
        assert self._db_path is not None
        conn = sqlite3.connect(self._db_path)
        try:
            for row in conn.execute(
                "SELECT id, agent_id, symbol, qty, entry_price, entry_date, "
                "entry_fill_id, remaining_qty, exit_fill_id, exit_date, "
                "exit_price, is_closed FROM lots"
            ):
                (lot_id, agent_id, symbol, qty, entry_price, entry_date_s,
                 entry_fill_id, remaining_qty, exit_fill_id, exit_date_s,
                 exit_price, is_closed) = row
                lot = Lot(
                    id=_to_uuid(lot_id),
                    agent_id=AgentId(agent_id),
                    symbol=symbol,
                    qty=Decimal(qty),
                    entry_price=Decimal(entry_price),
                    entry_date=date.fromisoformat(entry_date_s),
                    entry_fill_id=_to_uuid(entry_fill_id),
                    remaining_qty=Decimal(remaining_qty),
                    exit_fill_id=_to_uuid(exit_fill_id) if exit_fill_id else None,
                    exit_date=date.fromisoformat(exit_date_s) if exit_date_s else None,
                    exit_price=Decimal(exit_price) if exit_price else None,
                    is_closed=bool(is_closed),
                )
                self._lots[lot.id] = lot
            for (fid,) in conn.execute("SELECT fill_id FROM booked_fills"):
                self._booked.add(_to_uuid(fid))
        finally:
            conn.close()

    def _persist_lot(self, lot: Lot) -> None:
        if self._db_path is None:
            return
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO lots "
                "(id, agent_id, symbol, qty, entry_price, entry_date, "
                "entry_fill_id, remaining_qty, exit_fill_id, exit_date, "
                "exit_price, is_closed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(lot.id),
                    lot.agent_id.value if hasattr(lot.agent_id, "value") else str(lot.agent_id),
                    lot.symbol,
                    str(lot.qty),
                    str(lot.entry_price),
                    lot.entry_date.isoformat(),
                    str(lot.entry_fill_id),
                    str(lot.remaining_qty),
                    str(lot.exit_fill_id) if lot.exit_fill_id else None,
                    lot.exit_date.isoformat() if lot.exit_date else None,
                    str(lot.exit_price) if lot.exit_price is not None else None,
                    int(lot.is_closed),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _persist_booked(self, fill_id: FillId, side: OrderSide) -> None:
        if self._db_path is None:
            return
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO booked_fills (fill_id, side) VALUES (?, ?)",
                (str(fill_id), side.value),
            )
            conn.commit()
        finally:
            conn.close()

    def is_empty(self) -> bool:
        """True if no fills have ever been booked."""
        with self._lock:
            return not self._booked

    def replay_from_oms_store(self, oms_store: object) -> int:
        """Backfill lots from an OMSStore's FILL_RECEIVED events.

        Called once on cold start (when the lot DB is empty but the OMS log
        has historic fills that pre-date lot persistence). Idempotent via
        booked_fills, so re-running is a no-op.
        Returns the number of fills booked.
        """
        from execution.oms import _deserialize_fill  # noqa: PLC0415
        from execution.oms_store import EventKind  # noqa: PLC0415

        booked = 0
        for evt in oms_store.iter_all():  # type: ignore[attr-defined]
            if evt.kind != EventKind.FILL_RECEIVED:
                continue
            try:
                fill = _deserialize_fill(evt.payload)
            except Exception:
                continue
            before = len(self._booked)
            self.book_fill(fill)
            if len(self._booked) > before:
                booked += 1
        return booked

    # ── Idempotent fill booking ───────────────────────────────────────────────

    def book_fill(self, fill: Fill, method: LotMethod = LotMethod.FIFO) -> None:
        """Idempotent fill→lot booking. Safe to call on duplicate fill events.

        BUY → opens a new lot.
        SELL → consumes lots (FIFO/LIFO).
        Returns silently if this fill.id has already been booked.
        """
        with self._lock:
            if fill.id in self._booked:
                return
            self._booked.add(fill.id)
        self._persist_booked(fill.id, fill.side)

        if fill.side == OrderSide.BUY:
            self.open_lot(fill)
        else:
            try:
                self.close_lots(fill.agent_id, fill.symbol, fill.qty, fill, method=method)
            except ValueError:
                # Insufficient open qty — most likely because the matching BUY
                # was booked under a different fill stream (or the position was
                # established before persistence was wired in). Mark booked so
                # we don't retry on every reconciler tick.
                pass

    # ── Write operations ──────────────────────────────────────────────────────

    def open_lot(self, fill: Fill) -> Lot:
        """Create and register a new lot from a BUY fill."""
        if fill.side != OrderSide.BUY:
            raise ValueError(f"open_lot requires a BUY fill, got {fill.side}")
        lot = Lot(
            id=new_id(),
            agent_id=fill.agent_id,
            symbol=fill.symbol,
            qty=fill.qty,
            entry_price=fill.price,
            entry_date=fill.timestamp.date(),
            entry_fill_id=fill.id,
        )
        with self._lock:
            self._lots[lot.id] = lot
        self._persist_lot(lot)
        return lot

    def close_lots(
        self,
        agent_id: AgentId,
        symbol: str,
        qty: Decimal,
        exit_fill: Fill,
        method: LotMethod = LotMethod.FIFO,
    ) -> list[Lot]:
        """Consume `qty` shares from open lots; return the (updated) affected lots.

        FIFO: oldest entry date first.
        LIFO: newest entry date first.
        Raises ValueError if insufficient open qty.
        """
        if exit_fill.side != OrderSide.SELL:
            raise ValueError(f"close_lots requires a SELL fill, got {exit_fill.side}")

        with self._lock:
            candidates = [
                lot for lot in self._lots.values()
                if lot.agent_id == agent_id
                and lot.symbol == symbol
                and not lot.is_closed
                and lot.remaining_qty > Decimal("0")
            ]
            candidates.sort(
                key=lambda lot: lot.entry_date,
                reverse=(method == LotMethod.LIFO),
            )

            available = sum(lot.remaining_qty for lot in candidates)
            if qty > available:
                raise ValueError(
                    f"Cannot close {qty} of {symbol} for {agent_id}: "
                    f"only {available} available across {len(candidates)} lots"
                )

            exit_date = exit_fill.timestamp.date()
            remaining_to_close = qty
            affected: list[Lot] = []

            for lot in candidates:
                if remaining_to_close <= Decimal("0"):
                    break
                consume = min(lot.remaining_qty, remaining_to_close)
                new_remaining = lot.remaining_qty - consume
                is_closed = new_remaining == Decimal("0")
                updated = replace(
                    lot,
                    remaining_qty=new_remaining,
                    is_closed=is_closed,
                    exit_fill_id=exit_fill.id if is_closed else lot.exit_fill_id,
                    exit_date=exit_date if is_closed else lot.exit_date,
                    exit_price=exit_fill.price if is_closed else lot.exit_price,
                )
                self._lots[lot.id] = updated
                affected.append(updated)
                remaining_to_close -= consume

        for updated in affected:
            self._persist_lot(updated)
        return affected

    # ── Read operations ───────────────────────────────────────────────────────

    def open_lots(self, agent_id: AgentId, symbol: str) -> list[Lot]:
        """Return all open (not fully consumed) lots for agent+symbol."""
        with self._lock:
            return [
                lot for lot in self._lots.values()
                if lot.agent_id == agent_id
                and lot.symbol == symbol
                and not lot.is_closed
            ]

    def all_lots(self) -> list[Lot]:
        with self._lock:
            return list(self._lots.values())

    def total_open_qty(self, agent_id: AgentId, symbol: str) -> Decimal:
        return sum(
            (lot.remaining_qty for lot in self.open_lots(agent_id, symbol)), Decimal("0")
        )

    def lots_by_exit_fill(self, exit_fill_id: FillId) -> list[Lot]:
        """Return lots that were closed (fully or partially) by `exit_fill_id`.

        Used by the calibration pipeline to attribute realized P&L of a SELL
        fill back to the originating BUY intent so a Brier outcome can be
        recorded.
        """
        with self._lock:
            return [
                lot for lot in self._lots.values()
                if lot.exit_fill_id == exit_fill_id
            ]

    def open_qty_by_symbol(self, agent_id: AgentId) -> dict[str, Decimal]:
        """Aggregate per-symbol open qty for one agent across all its lots."""
        out: dict[str, Decimal] = {}
        with self._lock:
            for lot in self._lots.values():
                if lot.agent_id != agent_id or lot.is_closed:
                    continue
                out[lot.symbol] = out.get(lot.symbol, Decimal("0")) + lot.remaining_qty
        return {s: q for s, q in out.items() if q > Decimal("0")}
