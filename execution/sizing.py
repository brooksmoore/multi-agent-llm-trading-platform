"""Leverage caps and EWMA vol-targeting.

Blueprint §16 leverage system:
  effective_max_gross = base_max_gross × MASTER_CAPABILITY × vix_scalar × drawdown_scalar

Per-agent base_max_gross:
  Haiku   1.50×   (trend following — linear Sharpe scaling with leverage)
  Sonnet  1.25×   (multi-factor — moderate leverage benefit)
  Opus    1.00×   (concentrated — idiosyncratic risk doesn't diversify)

EWMA vol-targeting:
  σ²_t = λ × σ²_{t-1} + (1−λ) × r²_t   (λ=0.94, daily returns)
  annualized vol = σ_daily × √252  (floor 8%)
  target_leverage = target_annual_vol / current_annual_vol
  capped at 1.75× and ±10% day-over-day change
"""

from __future__ import annotations

from decimal import Decimal

from core.types import AgentId, DrawdownBucket, VixBucket

# ── Constants ─────────────────────────────────────────────────────────────────

EWMA_LAMBDA: Decimal = Decimal("0.94")
VOL_FLOOR_ANNUAL: Decimal = Decimal("0.08")          # 8% annual floor
VOL_TARGET_CAP_LEVERAGE: Decimal = Decimal("1.75")   # vol-target leverage cap
LEVERAGE_DAY_CHANGE_CAP: Decimal = Decimal("0.10")   # ±10% per day
TRADING_DAYS: Decimal = Decimal("252")
MAX_MASTER_CAPABILITY: Decimal = Decimal("1.5")
DEFAULT_MASTER_CAPABILITY: Decimal = Decimal("1.0")

_SQRT_252: Decimal = TRADING_DAYS.sqrt()
_FLOOR_DAILY: Decimal = VOL_FLOOR_ANNUAL / _SQRT_252

# ── Per-agent base leverage caps ──────────────────────────────────────────────

AGENT_BASE_MAX_GROSS: dict[AgentId, Decimal] = {
    AgentId.HAIKU:   Decimal("1.50"),
    AgentId.SONNET:  Decimal("1.25"),
    AgentId.OPUS:    Decimal("1.00"),
    AgentId.MANAGER: Decimal("1.00"),
}

# ── VIX ladder scalars ────────────────────────────────────────────────────────

VIX_SCALARS: dict[VixBucket, Decimal] = {
    VixBucket.VERY_LOW:   Decimal("0.6"),
    VixBucket.SWEET_SPOT: Decimal("1.0"),
    VixBucket.ELEVATED:   Decimal("0.8"),
    VixBucket.STRESS:     Decimal("0.5"),
    VixBucket.CRISIS:     Decimal("0.25"),
}

# ── Drawdown ladder scalars (per-agent sleeve drawdown) ───────────────────────

DRAWDOWN_SCALARS: dict[DrawdownBucket, Decimal] = {
    DrawdownBucket.NORMAL:      Decimal("1.0"),
    DrawdownBucket.YELLOW:      Decimal("0.75"),
    DrawdownBucket.ORANGE:      Decimal("0.50"),
    DrawdownBucket.RED:         Decimal("0.25"),
    DrawdownBucket.FORCED_CASH: Decimal("0.0"),
}


# ── VIX bucket classifier ─────────────────────────────────────────────────────


def classify_vix(vix: Decimal) -> VixBucket:
    """Map a VIX reading to the appropriate VixBucket."""
    if vix < Decimal("12"):
        return VixBucket.VERY_LOW
    if vix < Decimal("18"):
        return VixBucket.SWEET_SPOT
    if vix < Decimal("25"):
        return VixBucket.ELEVATED
    if vix < Decimal("35"):
        return VixBucket.STRESS
    return VixBucket.CRISIS


# ── Effective max gross leverage ──────────────────────────────────────────────


def effective_max_gross(
    agent_id: AgentId,
    master_capability: Decimal,
    vix_bucket: VixBucket,
    drawdown_bucket: DrawdownBucket,
) -> Decimal:
    """Compute the effective gross leverage cap for one agent.

    formula: base × MASTER_CAPABILITY × vix_scalar × drawdown_scalar

    Raises ValueError if master_capability > 1.5 (requires OVERRIDE_KEY).
    """
    if master_capability > MAX_MASTER_CAPABILITY:
        raise ValueError(
            f"MASTER_CAPABILITY {master_capability} exceeds {MAX_MASTER_CAPABILITY}: "
            "set OVERRIDE_KEY to allow values above 1.5"
        )
    base = AGENT_BASE_MAX_GROSS[agent_id]
    result = (
        base
        * master_capability
        * VIX_SCALARS[vix_bucket]
        * DRAWDOWN_SCALARS[drawdown_bucket]
    )
    return max(result, Decimal("0"))


# ── EWMA volatility estimator ─────────────────────────────────────────────────


class EWMAVolEstimator:
    """Exponentially-weighted moving average variance estimator (λ=0.94).

    Call `update(daily_return)` once per trading day with the day's return as a
    decimal fraction (e.g. 0.012 for +1.2%). The initial variance is zero until
    the first update; the floor prevents zero-vol leverage blow-ups.
    """

    def __init__(self, lambda_: Decimal = EWMA_LAMBDA) -> None:
        self._lambda = lambda_
        self._variance: Decimal = Decimal("0")

    def update(self, daily_return: Decimal) -> None:
        r2 = daily_return * daily_return
        self._variance = self._lambda * self._variance + (1 - self._lambda) * r2

    def daily_vol(self) -> Decimal:
        """Daily volatility estimate with 8%-annual floor applied."""
        if self._variance == Decimal("0"):
            return _FLOOR_DAILY
        raw = self._variance.sqrt()
        return max(raw, _FLOOR_DAILY)

    def annual_vol(self) -> Decimal:
        """Annualized volatility estimate (daily_vol × √252)."""
        return self.daily_vol() * _SQRT_252

    @property
    def variance(self) -> Decimal:
        return self._variance


# ── Vol-target sizer ──────────────────────────────────────────────────────────


class VolTargetSizer:
    """Converts a vol estimate into a target leverage ratio.

    target_leverage = target_annual_vol / current_annual_vol
    Capped at VOL_TARGET_CAP_LEVERAGE (1.75×).
    Day-over-day change capped at ±10% of the previous leverage.
    """

    def __init__(
        self,
        estimator: EWMAVolEstimator,
        target_annual_vol: Decimal = Decimal("0.10"),
    ) -> None:
        self._estimator = estimator
        self._target_vol = target_annual_vol
        self._prev_leverage: Decimal | None = None

    def target_leverage(self) -> Decimal:
        """Compute today's vol-target leverage.

        Call once per day after `estimator.update()`.
        """
        current_vol = self._estimator.annual_vol()  # already >= 8% floor
        raw = self._target_vol / current_vol

        # Cap at 1.75×
        capped = min(raw, VOL_TARGET_CAP_LEVERAGE)

        # ±10% day-over-day change cap
        if self._prev_leverage is not None:
            max_up = self._prev_leverage * (1 + LEVERAGE_DAY_CHANGE_CAP)
            max_down = self._prev_leverage * (1 - LEVERAGE_DAY_CHANGE_CAP)
            capped = min(max(capped, max_down), max_up)

        self._prev_leverage = capped
        return capped

    def final_leverage(
        self,
        max_gross: Decimal,
    ) -> Decimal:
        """Return min(vol_target_leverage, max_gross) — the binding constraint wins."""
        return min(self.target_leverage(), max_gross)
