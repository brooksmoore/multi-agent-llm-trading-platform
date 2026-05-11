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
from config.universes import estimated_slippage_bps
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
# New (planner-rebalance-delta): emitted when the delta between target and
# current position is within the rebalance no-op band, so we don't churn.
PLAN_REJECT_NEAR_TARGET: str = "unsized:near_target"
# New: BUY when current position is already AT OR ABOVE target. BUY is
# additive-only — if we're at/above target, the LLM should use REBALANCE_TO
# (or SELL) to trim. Surfaces LLM intent-vocabulary errors rather than
# silently over-buying as the pre-fix planner did.
PLAN_REJECT_ALREADY_AT_TARGET: str = "unsized:already_at_target"

_CLOSING_ACTIONS: frozenset[Action] = frozenset({Action.SELL, Action.CLOSE})

# Rebalance no-op band: if |target_notional - current_notional| / sleeve_equity
# is below this, we treat the intent as "already at target" and skip. Prevents
# hourly churn around a stable target weight.
_REBALANCE_BAND_PCT: Decimal = Decimal("0.02")  # 2pp of sleeve equity

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
        # planner-rebalance-delta counters
        self.dropped_near_target: int = 0
        self.dropped_already_at_target: int = 0

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

        # SELL with target_weight <= 0 means "fully exit this position" —
        # semantically equivalent to CLOSE. The pre-fix planner sized this
        # as a fresh $0 position, hit the sub-min floor, and silently
        # dropped the exit intent. Route to the close path instead so
        # trend-flip exits actually fire.
        if intent.action == Action.SELL and intent.target_weight <= Decimal("0"):
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

        target_notional = vol_targeted_position_value(
            target_weight=intent.target_weight,
            agent_equity=agent_state.sleeve_equity,
            realized_vol_annual=realized_vol,
            effective_vol_target=effective_vol_tgt,
        )

        gross_cap = emg * agent_state.sleeve_equity
        binding = "vol_target"
        if target_notional > gross_cap:
            target_notional = gross_cap
            binding = "max_gross"

        # ── Delta-aware sizing (planner-rebalance-delta fix) ─────────────────
        # The pre-fix planner sized BUY/REBALANCE_TO as a *fresh* position
        # equal to target_notional, ignoring any existing holdings. That
        # caused hourly Haiku cycles to re-buy the full target every cycle,
        # accumulating 4× the intended sleeve cap. The fix: every non-CLOSE
        # action computes the delta against existing lots and submits only
        # for the delta. Options legs (MLEG) are exempt — they're per-leg
        # opens, not weight-based rebalances.
        current_qty = (
            Decimal("0") if is_options
            else self._ledger.total_open_qty(intent.agent_id, symbol)
        )
        current_mark_for_delta = market_snapshot.current_prices.get(symbol)
        if (
            not is_options
            and current_mark_for_delta is not None
            and current_mark_for_delta > Decimal("0")
        ):
            current_notional = current_qty * current_mark_for_delta
        else:
            current_notional = Decimal("0")

        delta_notional = target_notional - current_notional

        # Direction guards by action verb:
        #   BUY            additive only; never produces a SELL order. If the
        #                  delta is negative the LLM should have used SELL or
        #                  REBALANCE_TO — surface that as ALREADY_AT_TARGET so
        #                  the calibration data shows the verb mismatch.
        #   REBALANCE_TO   bidirectional; positive delta → BUY, negative → SELL.
        #   SELL (w>0)     trim toward target; positive delta means we're
        #                  already at/below target — no-op via NEAR_TARGET.
        if intent.action == Action.BUY and delta_notional <= Decimal("0"):
            log.info(
                "planner: BUY %s/%s skipped — already at/above target "
                "(current=$%.2f target=$%.2f)",
                intent.agent_id, symbol, current_notional, target_notional,
            )
            self.dropped_already_at_target += 1
            return PLAN_REJECT_ALREADY_AT_TARGET
        if intent.action == Action.SELL and delta_notional >= Decimal("0"):
            log.info(
                "planner: SELL %s/%s skipped — already at/below trim target "
                "(current=$%.2f target=$%.2f)",
                intent.agent_id, symbol, current_notional, target_notional,
            )
            self.dropped_near_target += 1
            return PLAN_REJECT_NEAR_TARGET

        # No-op band: tiny rebalances aren't worth the friction.
        if (
            agent_state.sleeve_equity > Decimal("0")
            and abs(delta_notional) / agent_state.sleeve_equity < _REBALANCE_BAND_PCT
        ):
            log.info(
                "planner: %s %s/%s skipped — within rebalance band "
                "(|delta|=$%.2f, %.2f%% of sleeve)",
                intent.action, intent.agent_id, symbol,
                abs(delta_notional),
                (abs(delta_notional) / agent_state.sleeve_equity * Decimal("100")),
            )
            self.dropped_near_target += 1
            return PLAN_REJECT_NEAR_TARGET

        # The "position_value" we'll size the actual order against is the
        # absolute delta (NOT the full target). For options legs this
        # falls back to the legacy fresh-position sizing because options
        # don't have a meaningful "current position notional" in the
        # ledger (qty=0 always for spreads via this path).
        position_value = abs(delta_notional) if not is_options else target_notional

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

        # ── Order side from delta direction (planner-rebalance-delta) ────────
        # - SELL action: always sells.
        # - BUY action: always buys (we guarded above against negative-delta BUYs).
        # - REBALANCE_TO: positive delta → BUY, negative → SELL. The pre-fix
        #   code routed everything-non-closing to BUY, which made a REBALANCE_TO
        #   meant to trim silently turn into an ADD.
        # - CLOSE: handled by _plan_close earlier; never reaches here.
        if is_options:
            side = OrderSide.SELL if intent.action in _CLOSING_ACTIONS else OrderSide.BUY
        elif intent.action == Action.SELL:
            side = OrderSide.SELL
        elif intent.action == Action.REBALANCE_TO:
            side = OrderSide.SELL if delta_notional < Decimal("0") else OrderSide.BUY
        else:  # Action.BUY
            side = OrderSide.BUY

        # For SELL orders against an existing position, cap qty at what we
        # actually hold. Prevents going short on rounding or stale-mark drift.
        if side == OrderSide.SELL and not is_options and current_qty > Decimal("0"):
            qty = min(qty, current_qty)

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

        slip_bps = estimated_slippage_bps(symbol)

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
                estimated_slippage_bps=slip_bps,
            )
        )

        log.info(
            "planner: %s %s %s qty=%.6f notional=$%.2f binding=%s est_slip=%sbps",
            intent.agent_id,
            side,
            symbol,
            qty,
            position_value,
            binding,
            slip_bps,
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
                estimated_slippage_bps=estimated_slippage_bps(symbol),
            )
        )
        return order
