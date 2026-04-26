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

_CLOSING_ACTIONS: frozenset[Action] = frozenset({Action.SELL, Action.CLOSE})


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

    # ── Public API ────────────────────────────────────────────────────────────

    def plan(
        self,
        intent: Intent,
        agent_state: AgentState,
        market_snapshot: MarketSnapshot,
    ) -> Order | None:
        """Size intent and return a PENDING Order, or None if below minimum.

        Returns None when:
        - Current mark price is missing or ≤ 0.
        - Resulting notional < $1 (sub-minimum).
        - Resulting qty rounds to 0 (e.g. FORCED_CASH drawdown bucket).

        Does NOT submit to OMS — caller owns that step.
        """
        # CLOSE intents bypass weight-based sizing — use actual open qty.
        if intent.action == Action.CLOSE:
            return self._plan_close(intent, agent_state, market_snapshot)

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

        realized_vol = market_snapshot.realized_vol_30d.get(
            intent.symbol, Decimal("0.08")
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
            return None

        # ── Mark price + qty ──────────────────────────────────────────────────
        current_mark = market_snapshot.current_prices.get(intent.symbol)
        if current_mark is None or current_mark <= Decimal("0"):
            log.warning("planner: no mark for %s — cannot size", intent.symbol)
            return None

        if is_options:
            # Whole contracts only; 1 contract = 100 underlying shares.
            qty = (position_value / (current_mark * _OPTIONS_MULTIPLIER)).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
        else:
            # Equities / crypto: fractional supported.
            qty = position_value / current_mark

        if qty <= Decimal("0"):
            return None

        side = OrderSide.SELL if intent.action in _CLOSING_ACTIONS else OrderSide.BUY
        order_class = OrderClass.MLEG if is_options else OrderClass.SIMPLE

        order = Order(
            id=new_id(),
            intent_id=intent.id,
            agent_id=intent.agent_id,
            symbol=intent.symbol,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
            order_class=order_class,
            time_in_force=TimeInForce.DAY,
            state=OrderState.PENDING,
            created_at=datetime.now(UTC),
            legs=intent.legs,
            is_letf=intent.symbol in LETF_WHITELIST,
        )

        self._bus.publish(
            IntentSizedEvent(
                intent_id=intent.id,
                agent_id=intent.agent_id,
                symbol=intent.symbol,
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
            intent.symbol,
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
    ) -> Order | None:
        """Plan a CLOSE intent: sell exactly the current open lot qty."""
        open_qty = self._ledger.total_open_qty(intent.agent_id, intent.symbol)
        if open_qty <= Decimal("0"):
            log.info(
                "planner: CLOSE %s/%s — no open lots, skipping",
                intent.agent_id,
                intent.symbol,
            )
            return None

        current_mark = market_snapshot.current_prices.get(intent.symbol)
        if current_mark is None or current_mark <= Decimal("0"):
            log.warning("planner: no mark for %s — cannot size CLOSE", intent.symbol)
            return None

        notional = open_qty * current_mark
        if notional < _MIN_NOTIONAL:
            return None

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
            symbol=intent.symbol,
            side=OrderSide.SELL,
            qty=open_qty,
            order_type=OrderType.MARKET,
            order_class=OrderClass.SIMPLE,
            time_in_force=TimeInForce.DAY,
            state=OrderState.PENDING,
            created_at=datetime.now(UTC),
            is_letf=intent.symbol in LETF_WHITELIST,
        )

        self._bus.publish(
            IntentSizedEvent(
                intent_id=intent.id,
                agent_id=intent.agent_id,
                symbol=intent.symbol,
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
