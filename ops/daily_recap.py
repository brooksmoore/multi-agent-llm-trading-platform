"""DailyRecap — single end-of-day Telegram message summarizing the bot's day.

Replaces real-time push for routine ops events (recon breaks, budget exhaust,
benchings, rotations, drawdown bucket changes, transient kill-switch trips,
fills). Subscribes to the EventBus, buffers events with timestamps, and exposes
`send(account, positions, lots, manager)` for the scheduler to invoke at 17:00 ET.

Real-time Telegram is reserved for stuck halts (>30 min) — handled in
ops/alerts.py, not here.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from core.events import (
    AgentBenchedEvent,
    BudgetExhaustedEvent,
    DrawdownLadderFiredEvent,
    Event,
    EventBus,
    FillReceivedEvent,
    KillSwitchResetEvent,
    KillSwitchTrippedEvent,
    LeverageRotationFlagEvent,
    ReconciliationBreakEvent,
)
from ops.telegram import TelegramAdapter

log = logging.getLogger(__name__)


@dataclass
class _Buffers:
    """Per-day accumulator. Cleared at the end of each recap."""
    recon_breaks: list[tuple[datetime, str, Decimal]] = field(default_factory=list)
    halts: list[tuple[datetime, str, str]] = field(default_factory=list)  # ts, state, reason
    halt_resets: int = 0
    budget_exhausted: list[tuple[datetime, Decimal]] = field(default_factory=list)
    benchings: list[tuple[datetime, str, int]] = field(default_factory=list)
    rotations: list[tuple[datetime, str, str]] = field(default_factory=list)
    drawdown_buckets: list[tuple[datetime, str, str, Decimal]] = field(default_factory=list)
    fill_count: int = 0
    fills_by_agent: Counter[str] = field(default_factory=Counter)


class DailyRecap:
    """Buffers ops events and emits a single Telegram message per day."""

    def __init__(
        self,
        bus: EventBus,
        telegram: TelegramAdapter | None,
    ) -> None:
        self._bus = bus
        self._telegram = telegram
        self._lock = threading.Lock()
        self._buf = _Buffers()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._bus.subscribe("reconciliation.break", self._on_recon)
        self._bus.subscribe("kill_switch.tripped", self._on_halt)
        self._bus.subscribe("kill_switch.reset", self._on_halt_reset)
        self._bus.subscribe("budget.exhausted", self._on_budget)
        self._bus.subscribe("agent.benched", self._on_bench)
        self._bus.subscribe("leverage.rotation_flag", self._on_rotation)
        self._bus.subscribe("drawdown.ladder_fired", self._on_drawdown)
        self._bus.subscribe("fill.received", self._on_fill)
        log.info("DailyRecap: started (telegram=%s)",
                 "enabled" if (self._telegram and self._telegram.enabled) else "disabled")

    def stop(self) -> None:
        self._bus.unsubscribe("reconciliation.break", self._on_recon)
        self._bus.unsubscribe("kill_switch.tripped", self._on_halt)
        self._bus.unsubscribe("kill_switch.reset", self._on_halt_reset)
        self._bus.unsubscribe("budget.exhausted", self._on_budget)
        self._bus.unsubscribe("agent.benched", self._on_bench)
        self._bus.unsubscribe("leverage.rotation_flag", self._on_rotation)
        self._bus.unsubscribe("drawdown.ladder_fired", self._on_drawdown)
        self._bus.unsubscribe("fill.received", self._on_fill)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_recon(self, event: Event) -> None:
        if not isinstance(event, ReconciliationBreakEvent):
            return
        with self._lock:
            self._buf.recon_breaks.append((datetime.now(UTC), event.symbol, abs(event.delta_usd)))

    def _on_halt(self, event: Event) -> None:
        if not isinstance(event, KillSwitchTrippedEvent):
            return
        with self._lock:
            self._buf.halts.append((datetime.now(UTC), str(event.new_state), event.reason))

    def _on_halt_reset(self, event: Event) -> None:
        if not isinstance(event, KillSwitchResetEvent):
            return
        with self._lock:
            self._buf.halt_resets += 1

    def _on_budget(self, event: Event) -> None:
        if not isinstance(event, BudgetExhaustedEvent):
            return
        with self._lock:
            self._buf.budget_exhausted.append((datetime.now(UTC), event.spent_today))

    def _on_bench(self, event: Event) -> None:
        if not isinstance(event, AgentBenchedEvent):
            return
        with self._lock:
            self._buf.benchings.append(
                (datetime.now(UTC), str(event.agent_id), event.consecutive_losses)
            )

    def _on_rotation(self, event: Event) -> None:
        if not isinstance(event, LeverageRotationFlagEvent):
            return
        with self._lock:
            self._buf.rotations.append(
                (datetime.now(UTC), str(event.agent_id), event.symbol)
            )

    def _on_drawdown(self, event: Event) -> None:
        if not isinstance(event, DrawdownLadderFiredEvent):
            return
        if event.new_bucket in ("normal", "yellow", ""):
            return
        with self._lock:
            self._buf.drawdown_buckets.append(
                (datetime.now(UTC), str(event.agent_id),
                 event.new_bucket, event.drawdown_pct)
            )

    def _on_fill(self, event: Event) -> None:
        if not isinstance(event, FillReceivedEvent):
            return
        with self._lock:
            self._buf.fill_count += 1
            self._buf.fills_by_agent[str(event.fill.agent_id)] += 1

    # ── Recap emit ────────────────────────────────────────────────────────────

    def snapshot_and_clear(self) -> _Buffers:
        """Atomically read and reset buffers. Exposed for tests."""
        with self._lock:
            buf = self._buf
            self._buf = _Buffers()
        return buf

    def format(
        self,
        buf: _Buffers,
        *,
        nav: Decimal | None = None,
        cash: Decimal | None = None,
        prior_nav: Decimal | None = None,
        positions: list[Any] | None = None,
        sleeve_pnl: dict[str, Decimal] | None = None,
        kill_state: str = "OK",
        budget_spent: Decimal | None = None,
    ) -> str:
        """Render a plain-text recap. Markdown escaping is handled by the
        adapter's `send_daily_recap()` wrapper."""
        lines: list[str] = []
        date_str = datetime.now().strftime("%Y-%m-%d")
        lines.append(f"Daily recap — {date_str}")
        lines.append("")

        # P&L block
        if nav is not None:
            lines.append(f"NAV: ${float(nav):,.2f}")
            if prior_nav is not None and prior_nav > 0:
                delta = nav - prior_nav
                pct = float(delta / prior_nav * 100)
                sign = "+" if delta >= 0 else ""
                lines.append(f"Day P&L: {sign}${float(delta):,.2f} ({sign}{pct:.2f}%)")
            if cash is not None:
                lines.append(f"Cash: ${float(cash):,.2f}")
        lines.append(f"Status: {kill_state}")
        if budget_spent is not None:
            lines.append(f"LLM spend today: ${float(budget_spent):.3f}")

        # Sleeve attribution
        if sleeve_pnl:
            lines.append("")
            lines.append("Sleeve P&L:")
            for sleeve, pnl in sorted(sleeve_pnl.items()):
                sign = "+" if pnl >= 0 else ""
                lines.append(f"  {sleeve}: {sign}${float(pnl):,.2f}")

        # Positions count (full list bloats the message; one-liner is enough)
        if positions is not None:
            lines.append("")
            lines.append(f"Positions: {len(positions)} open")

        # Fills
        if buf.fill_count:
            lines.append("")
            agents = ", ".join(
                f"{a}={n}" for a, n in sorted(buf.fills_by_agent.items())
            )
            lines.append(f"Fills: {buf.fill_count} ({agents})")

        # Ops events
        ops_lines: list[str] = []
        if buf.halts:
            states = Counter(s for _, s, _ in buf.halts)
            states_str = ", ".join(f"{s}×{n}" for s, n in states.most_common())
            ops_lines.append(
                f"Halts: {len(buf.halts)} ({states_str}); {buf.halt_resets} cleared"
            )
        if buf.recon_breaks:
            max_delta = max(d for _, _, d in buf.recon_breaks)
            syms = Counter(s for _, s, _ in buf.recon_breaks)
            top = ", ".join(f"{s}×{n}" for s, n in syms.most_common(3))
            ops_lines.append(
                f"Recon breaks: {len(buf.recon_breaks)} "
                f"(max ${float(max_delta):.2f}; {top})"
            )
        if buf.budget_exhausted:
            ops_lines.append(f"Budget exhausted: {len(buf.budget_exhausted)}×")
        if buf.benchings:
            agents = ", ".join(a for _, a, _ in buf.benchings)
            ops_lines.append(f"Benchings: {agents}")
        if buf.rotations:
            ops_lines.append(f"LETF rotations: {len(buf.rotations)}")
        if buf.drawdown_buckets:
            worst = max(
                buf.drawdown_buckets,
                key=lambda x: {"orange": 1, "red": 2, "forced_cash": 3}.get(x[2], 0),
            )
            ops_lines.append(
                f"Drawdown bucket: {worst[1]} → {worst[2]} "
                f"({float(worst[3]) * 100:.1f}%)"
            )

        if ops_lines:
            lines.append("")
            lines.append("Ops:")
            lines.extend(f"  {ln}" for ln in ops_lines)
        else:
            lines.append("")
            lines.append("Ops: clean day, no events")

        return "\n".join(lines)

    def send(
        self,
        *,
        nav: Decimal | None = None,
        cash: Decimal | None = None,
        prior_nav: Decimal | None = None,
        positions: list[Any] | None = None,
        sleeve_pnl: dict[str, Decimal] | None = None,
        kill_state: str = "OK",
        budget_spent: Decimal | None = None,
    ) -> bool:
        """Format + send the daily recap, then clear buffers.

        Always clears buffers, even if Telegram is disabled or the send
        fails — we don't want yesterday's events bleeding into tomorrow.
        """
        buf = self.snapshot_and_clear()
        body = self.format(
            buf, nav=nav, cash=cash, prior_nav=prior_nav, positions=positions,
            sleeve_pnl=sleeve_pnl, kill_state=kill_state, budget_spent=budget_spent,
        )
        if self._telegram is None or not self._telegram.enabled:
            log.info("DailyRecap [telegram disabled]:\n%s", body)
            return False
        return self._telegram.send_daily_recap(body)
