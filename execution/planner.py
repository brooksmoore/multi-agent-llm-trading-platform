"""ExecutionPlanner — translates approved Intent objects into sized Orders.

Blueprint §16 sizing math:
  effective_max_gross  = base_max_gross[agent]  × MC × vix_scalar × dd_scalar
  effective_vol_target = base_vol_target[agent] × MC
  position_value = vol_targeted_position_value(
      target_weight, agent_equity, realized_vol_30d, effective_vol_target
  )
  position_value = min(position_value, effective_max_gross × agent_equity)
  qty = position_value / current_mark   (fractional for non-options)

All math is pure Python.  No LLM involvement past the intent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from config.runtime_store import runtime_store
from core.events import EventBus, IntentSizedEvent
from core.types import (
    Action,
    AgentState,
    Intent,
    MarketSnapshot,
    Order,
    OrderClass,
    OrderSide,
    OrderState,
    OrderType,
    TimeInForce,
    new_id,
    normalize_symbol,
)
from execution.lots import LotLedger
from execution.oms import OMS
from execution.risk import LETF_WHITELIST
from execution.sizing import (
    AGENT_BASE_VOL_TARGET,
    effective_max_gross,
    vol_targeted_position_value,
)

log = logging.getLogger(__name__)

_MIN_NOTIONAL: Decimal = Decimal("1.00")
_OPTIONS_MULTIPLIER: Decimal = Decimal("100")   # 1 equity-options contract = 100 shares
# Alpaca caps fractional qty at 9dp; submitting more precision is silently
# truncated by the broker, leaving the OMS unable to reconcile filled_qty.
_FRACTIONAL_QTY_PRECISION: Decimal = Decimal("0.000000001")

# Planner rejection reasons. Returned as outcome strings so callers
# (and the dashboard) can distinguish "skipped sub-$1" from "agent
# hallucinated ownership" from "no market data". Prefixed `unsized:` to
# preserve compatibility with existing `unsized` outcome consumers.
PLAN_REJECT_SUB_MIN: str = "unsized:sub_min"
PLAN_REJECT_NO_POSITION: str = "unsized:no_position"
PLAN_REJECT_NO_MARK: str = "unsized:no_mark"
PLAN_REJECT_ZERO_QTY: str = "unsized:zero_qty"

_CLOSING_ACTIONS: frozenset[Action] = frozenset({Action.SELL, Action.CLOSE})

_normalize_symbol = normalize_symbol  # legacy alias for in-module readability


class ExecutionPlanner:
    """Convert RiskGate-approved intents into PENDING Order objects.

    Caller is responsible for submitting the returned Order via OMS.submit_order().
    """

    def __init__(
        self,
        oms: OMS,
        lot_ledger: LotLedger,
        bus: EventBus,
    ) -> None:
        self._oms = oms
        self._ledger = lot_ledger
        self._bus = bus
        # Counters for visibility into intents dropped at planning. Read-only
        # from outside; reset() clears them. Surfaced via /metrics or logs.
        self.dropped_no_mark: int = 0
        self.dropped_no_position: int = 0
        self.dropped_sub_min: int = 0
        self.dropped_zero_qty: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def plan(
        self,
        intent: Intent,
        agent_state: AgentState,
        market_snapshot: MarketSnapshot,
    ) -> Order | str:
        """Size intent and return a PENDING Order, or a `PLAN_REJECT_*` reason
        string when the intent is dropped for sub-min notional, missing market
        data, or invalid state.

        Returns None when:
        - Current mark price is missing or ≤ 0.
        - Resulting notional < $1 (sub-minimum).
        - Resulting qty rounds to 0 (e.g. FORCED_CASH drawdown bucket).

        Does NOT submit to OMS — caller owns that step.
        """
        # CLOSE intents bypass weight-based sizing — use actual open qty.
        if intent.action == Action.CLOSE:
            return self._plan_close(intent, agent_state, market_snapshot)

        # SELL fail-safe: refuse to size a SELL for a symbol the agent has
        # never bought. Without this guard, an LLM that hallucinates ownership
        # (e.g. seeing another sleeve's position in its context) could place
        # an unauthorized sell. CLOSE has its own analogous check below.
        if intent.action == Action.SELL:
            check_symbol = _normalize_symbol(intent.symbol)
            held = self._ledger.total_open_qty(intent.agent_id, check_symbol)
            if held <= Decimal("0"):
                log.warning(
                    "planner: SELL %s/%s rejected — agent has no open lots",
                    intent.agent_id, check_symbol,
                )
                self.dropped_no_position += 1
                return PLAN_REJECT_NO_POSITION

        # Read MC dynamically so dashboard-slider changes take effect immediately.
        mc = runtime_store.master_capability

        is_options = bool(intent.legs)

        # ── Blueprint §16 sizing ──────────────────────────────────────────────
        emg = effective_max_gross(
            agent_id=intent.agent_id,
            master_capability=mc,
            vix_bucket=market_snapshot.vix_bucket,
            drawdown_bucket=agent_state.drawdown_bucket,
        )

        effective_vol_tgt = AGENT_BASE_VOL_TARGET[intent.agent_id] * mc

        symbol = _normalize_symbol(intent.symbol)
        realized_vol = market_snapshot.realized_vol_30d.get(
            symbol, Decimal("0.08")
        )

        position_value = vol_targeted_position_value(
            target_weight=intent.target_weight,
            agent_equity=agent_state.sleeve_equity,
            realized_vol_annual=realized_vol,
            effective_vol_target=effective_vol_tgt,
        )

        gross_cap = emg * agent_state.sleeve_equity
        binding = "vol_target"
        if position_value > gross_cap:
            position_value = gross_cap
            binding = "max_gross"

        if position_value < _MIN_NOTIONAL:
            log.debug(
                "planner: sub-$1 notional %.4f for %s/%s — skipping",
                position_value,
                intent.agent_id,
                intent.symbol,
            )
            self.dropped_sub_min += 1
            return PLAN_REJECT_SUB_MIN

        # ── Mark price + qty ──────────────────────────────────────────────────
        current_mark = market_snapshot.current_prices.get(symbol)
        if current_mark is None or current_mark <= Decimal("0"):
            # ERROR (not WARNING): a missing mark for an intended symbol means
            # the agent's universe and the data layer's fetched-symbols are out
            # of sync — a config/data bug, not a normal rejection. This is the
            # silent-drop path that hid intents in early runs.
            self.dropped_no_mark += 1
            log.error(
                "planner: no mark for %s — cannot size (agent=%s action=%s); "
                "dropped_no_mark_total=%d",
                symbol, intent.agent_id, intent.action, self.dropped_no_mark,
            )
            return PLAN_REJECT_NO_MARK

        if is_options:
            # Whole contracts only; 1 contract = 100 underlying shares.
            qty = (position_value / (current_mark * _OPTIONS_MULTIPLIER)).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
        else:
            # Equities / crypto: fractional supported, rounded to broker precision.
            qty = (position_value / current_mark).quantize(
                _FRACTIONAL_QTY_PRECISION, rounding=ROUND_DOWN
            )

        if qty <= Decimal("0"):
            self.dropped_zero_qty += 1
            return PLAN_REJECT_ZERO_QTY

        side = OrderSide.SELL if intent.action in _CLOSING_ACTIONS else OrderSide.BUY
        order_class = OrderClass.MLEG if is_options else OrderClass.SIMPLE

        order = Order(
            id=new_id(),
            intent_id=intent.id,
            agent_id=intent.agent_id,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
            order_class=order_class,
            time_in_force=TimeInForce.DAY,
            state=OrderState.PENDING,
            created_at=datetime.now(UTC),
            legs=intent.legs,
            is_letf=symbol in LETF_WHITELIST,
        )

        self._bus.publish(
            IntentSizedEvent(
                intent_id=intent.id,
                agent_id=intent.agent_id,
                symbol=symbol,
                target_weight=intent.target_weight,
                position_value_usd=position_value,
                qty=qty,
                effective_vol_target=effective_vol_tgt,
                effective_max_gross_val=emg,
                realized_vol_30d=realized_vol,
                binding_constraint=binding,
            )
        )

        log.info(
            "planner: %s %s %s qty=%.6f notional=$%.2f binding=%s",
            intent.agent_id,
            side,
            symbol,
            qty,
            position_value,
            binding,
        )
        return order

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _plan_close(
        self,
        intent: Intent,
        agent_state: AgentState,
        market_snapshot: MarketSnapshot,
    ) -> Order | str:
        """Plan a CLOSE intent: sell exactly the current open lot qty."""
        symbol = _normalize_symbol(intent.symbol)
        open_qty = self._ledger.total_open_qty(intent.agent_id, symbol)
        if open_qty <= Decimal("0"):
            log.info(
                "planner: CLOSE %s/%s — no open lots, skipping",
                intent.agent_id,
                symbol,
            )
            self.dropped_no_position += 1
            return PLAN_REJECT_NO_POSITION

        current_mark = market_snapshot.current_prices.get(symbol)
        if current_mark is None or current_mark <= Decimal("0"):
            self.dropped_no_mark += 1
            log.error(
                "planner: no mark for %s — cannot size CLOSE (agent=%s); "
                "dropped_no_mark_total=%d",
                symbol, intent.agent_id, self.dropped_no_mark,
            )
            return PLAN_REJECT_NO_MARK

        notional = open_qty * current_mark
        if notional < _MIN_NOTIONAL:
            self.dropped_sub_min += 1
            return PLAN_REJECT_SUB_MIN

        mc = runtime_store.master_capability
        emg = effective_max_gross(
            agent_id=intent.agent_id,
            master_capability=mc,
            vix_bucket=market_snapshot.vix_bucket,
            drawdown_bucket=agent_state.drawdown_bucket,
        )
        effective_vol_tgt = AGENT_BASE_VOL_TARGET[intent.agent_id] * mc

        order = Order(
            id=new_id(),
            intent_id=intent.id,
            agent_id=intent.agent_id,
            symbol=symbol,
            side=OrderSide.SELL,
            qty=open_qty,
            order_type=OrderType.MARKET,
            order_class=OrderClass.SIMPLE,
            time_in_force=TimeInForce.DAY,
            state=OrderState.PENDING,
            created_at=datetime.now(UTC),
            is_letf=symbol in LETF_WHITELIST,
        )

        self._bus.publish(
            IntentSizedEvent(
                intent_id=intent.id,
                agent_id=intent.agent_id,
                symbol=symbol,
                target_weight=Decimal("0"),
                position_value_usd=notional,
                qty=open_qty,
                effective_vol_target=effective_vol_tgt,
                effective_max_gross_val=emg,
                realized_vol_30d=Decimal("0.08"),
                binding_constraint="close",
            )
        )
        return order
