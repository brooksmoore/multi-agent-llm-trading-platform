"""Pre-trade RiskGate: all checks before an Intent becomes an Order.

Every Intent must pass check_intent() before ExecutionPlanner creates an Order.
The gate is deterministic (no LLM, no broker calls) and runs in microseconds.

Check order (first failure wins):
  1. Kill switch LIQUIDATE → only SELL/CLOSE allowed
  2. Kill switch blocks new entries → BUY/REBALANCE_TO rejected
  3. Agent benched → all intents rejected
  4. Per-agent FORCED_CASH drawdown bucket → BUY/REBALANCE_TO rejected
  5. LETF whitelist check → non-whitelist leveraged names rejected
  6. LETF hold-period check → existing position overdue → buy rejected
  7. Options: exposure cap (20% of sleeve equity)
  8. Single-name weight cap → capped (allowed=True with capped_weight)
  9. effective_gross == 0 → BUY/REBALANCE_TO rejected
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.types import (
    Action,
    AgentId,
    AgentState,
    DrawdownBucket,
    Intent,
    KillSwitchState,
    Position,
    Sleeve,
)
from execution.kill_switch import KillSwitchEngine
from execution.lots import LotLedger
from execution.tax import WashSaleChecker

# ── Per-agent single-name weight caps ─────────────────────────────────────────

AGENT_SINGLE_NAME_CAPS: dict[AgentId, Decimal] = {
    AgentId.HAIKU:   Decimal("0.25"),   # 25% per ETF
    AgentId.SONNET:  Decimal("0.12"),   # 12% per name
    AgentId.OPUS:    Decimal("0.18"),   # 18% per name (concentrated mandate)
    AgentId.MANAGER: Decimal("0.30"),   # manager has wider mandate
}

# ── LETF policy ───────────────────────────────────────────────────────────────

LETF_WHITELIST: frozenset[str] = frozenset({
    "TQQQ", "SQQQ", "UPRO", "SPXU", "SOXL", "SOXS", "TMF", "TMV",
})
LETF_MAX_HOLD_CALENDAR_DAYS: int = 5

# ── Options policy ────────────────────────────────────────────────────────────

OPTIONS_MAX_SLEEVE_FRACTION: Decimal = Decimal("0.20")

# ── Actions that open new positions ──────────────────────────────────────────

_OPENING_ACTIONS: frozenset[Action] = frozenset({Action.BUY, Action.REBALANCE_TO})
_CLOSING_ACTIONS: frozenset[Action] = frozenset({Action.SELL, Action.CLOSE})


# ── Result type ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RiskDecision:
    """Result of a RiskGate check.

    allowed=True  → intent may proceed (capped_weight may be set if we reduced it).
    allowed=False → veto_reason explains why.
    """

    allowed: bool
    veto_reason: str | None = None
    capped_weight: Decimal | None = None


# ── RiskGate ──────────────────────────────────────────────────────────────────


class RiskGate:
    """Pre-trade guard. check_intent() must return allowed=True before Order creation."""

    def __init__(
        self,
        kill_engine: KillSwitchEngine,
        wash_checker: WashSaleChecker,
        lot_ledger: LotLedger,
    ) -> None:
        self._kill = kill_engine
        self._wash = wash_checker
        self._lots = lot_ledger

    def check_intent(
        self,
        intent: Intent,
        agent_state: AgentState,
        effective_gross: Decimal,
        positions: list[Position],
        ts: datetime,
    ) -> RiskDecision:
        """Run all pre-trade checks. Returns the first failure or allowed=True."""
        ks = self._kill.state

        # 1. Kill switch LIQUIDATE → only closes allowed
        if ks == KillSwitchState.DRAWDOWN_LIQUIDATE and intent.action not in _CLOSING_ACTIONS:
            return RiskDecision(
                allowed=False,
                veto_reason=f"kill_switch:{ks} — only sell/close allowed",
            )

        # 2. Kill switch blocks new entries
        if intent.action in _OPENING_ACTIONS and not self._kill.can_open_new():
            return RiskDecision(
                allowed=False,
                veto_reason=f"kill_switch:{ks} — new entries blocked",
            )

        # 3. Agent benched
        if self._kill.is_agent_benched(agent_state.agent_id, ts):
            return RiskDecision(
                allowed=False,
                veto_reason=(
                    f"agent:{agent_state.agent_id} is benched "
                    "(5 consecutive losses — 24h cooldown)"
                ),
            )

        # 4. Per-agent FORCED_CASH drawdown → no buys
        if (
            agent_state.drawdown_bucket == DrawdownBucket.FORCED_CASH
            and intent.action in _OPENING_ACTIONS
        ):
            return RiskDecision(
                allowed=False,
                veto_reason="drawdown_bucket:FORCED_CASH — no new buys; sleeve down >25%",
            )

        # 5 & 6. LETF checks
        if intent.sleeve == Sleeve.EQUITY and intent.symbol in LETF_WHITELIST:
            letf_decision = self._check_letf(intent, ts)
            if letf_decision is not None:
                return letf_decision

        # 7. Options exposure cap
        if intent.sleeve == Sleeve.OPTIONS:
            options_decision = self._check_options(intent, positions, agent_state.sleeve_equity)
            if options_decision is not None:
                return options_decision

        # 8. Single-name weight cap (soft cap — allowed but capped)
        cap = AGENT_SINGLE_NAME_CAPS.get(intent.agent_id)
        if cap is not None and intent.target_weight > cap:
            return RiskDecision(allowed=True, capped_weight=cap)

        # 9. Effective gross == 0 → no buys
        if effective_gross == Decimal("0") and intent.action in _OPENING_ACTIONS:
            return RiskDecision(
                allowed=False,
                veto_reason="effective_gross=0 — all leverage caps are zero",
            )

        return RiskDecision(allowed=True)

    def check_letf_auto_liquidations(
        self,
        agent_id: AgentId,
        positions: list[Position],
        ts: datetime,
    ) -> list[str]:
        """Return symbols of LETFs that must be auto-liquidated (held > 5 days).

        Call once per trading day before the market opens.
        """
        today = ts.date()
        to_liquidate: list[str] = []
        for pos in positions:
            if pos.agent_id != agent_id or pos.symbol not in LETF_WHITELIST:
                continue
            open_lots = self._lots.open_lots(agent_id, pos.symbol)
            if not open_lots:
                continue
            oldest_entry = min(lot.entry_date for lot in open_lots)
            days_held = (today - oldest_entry).days
            if days_held > LETF_MAX_HOLD_CALENDAR_DAYS:
                to_liquidate.append(pos.symbol)
        return to_liquidate

    # ── Private helpers ───────────────────────────────────────────────────────

    def _check_letf(self, intent: Intent, ts: datetime) -> RiskDecision | None:
        """Check LETF hold-period constraint for buy intents."""
        if intent.action in _CLOSING_ACTIONS:
            return None  # sells are always allowed (may be the auto-liquidation)
        today = ts.date()
        open_lots = self._lots.open_lots(intent.agent_id, intent.symbol)
        if open_lots:
            oldest = min(lot.entry_date for lot in open_lots)
            days_held = (today - oldest).days
            if days_held > LETF_MAX_HOLD_CALENDAR_DAYS:
                return RiskDecision(
                    allowed=False,
                    veto_reason=(
                        f"letf:{intent.symbol} already held {days_held} days "
                        "(max {LETF_MAX_HOLD_CALENDAR_DAYS}); liquidate before re-entry"
                    ),
                )
        return None

    def _check_options(
        self,
        intent: Intent,
        positions: list[Position],
        sleeve_equity: Decimal,
    ) -> RiskDecision | None:
        """Enforce the 20%-of-sleeve options exposure cap."""
        if sleeve_equity == Decimal("0"):
            return None
        options_value = sum(
            abs(p.market_value)
            for p in positions
            if p.agent_id == intent.agent_id and p.sleeve == Sleeve.OPTIONS
        )
        current_fraction = options_value / sleeve_equity
        new_fraction = current_fraction + intent.target_weight
        if new_fraction > OPTIONS_MAX_SLEEVE_FRACTION:
            return RiskDecision(
                allowed=False,
                veto_reason=(
                    f"options:{intent.symbol} would push options to "
                    f"{new_fraction:.1%} of sleeve (max {OPTIONS_MAX_SLEEVE_FRACTION:.0%})"
                ),
            )
        return None
