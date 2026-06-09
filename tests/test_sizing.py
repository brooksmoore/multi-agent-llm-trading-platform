"""Tests for execution/sizing.py — EWMA vol-targeting and leverage caps."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.types import AgentId, DrawdownBucket, VixBucket
from execution.sizing import (
    DEFAULT_MASTER_CAPABILITY,
    VOL_FLOOR_ANNUAL,
    VOL_TARGET_CAP_LEVERAGE,
    EWMAVolEstimator,
    VolTargetSizer,
    classify_vix,
    effective_max_gross,
)

# ── EWMAVolEstimator ──────────────────────────────────────────────────────────


def test_initial_vol_uses_floor() -> None:
    est = EWMAVolEstimator()
    # No data → variance is 0 → floor kicks in
    assert est.annual_vol() == VOL_FLOOR_ANNUAL


def test_annual_vol_at_least_floor() -> None:
    est = EWMAVolEstimator()
    # Feed in near-zero returns → vol estimate still at floor
    for _ in range(100):
        est.update(Decimal("0.0001"))
    assert est.annual_vol() >= VOL_FLOOR_ANNUAL


def test_ewma_lambda_decay() -> None:
    est = EWMAVolEstimator()
    # Large shock then zeros → variance should decay
    est.update(Decimal("0.10"))  # 10% daily return
    high_var = est.variance
    for _ in range(50):
        est.update(Decimal("0"))
    assert est.variance < high_var * Decimal("0.1")


def test_vol_estimate_increases_with_large_returns() -> None:
    est = EWMAVolEstimator()
    for _ in range(30):
        est.update(Decimal("0.01"))  # 1%/day calm period
    calm_vol = est.annual_vol()
    for _ in range(10):
        est.update(Decimal("0.05"))  # 5%/day shock
    volatile_vol = est.annual_vol()
    assert volatile_vol > calm_vol


def test_daily_vol_times_sqrt252_equals_annual() -> None:
    est = EWMAVolEstimator()
    for _ in range(20):
        est.update(Decimal("0.02"))
    daily = est.daily_vol()
    annual = est.annual_vol()
    # annual = daily × √252
    sqrt252 = Decimal("252").sqrt()
    assert abs(annual - daily * sqrt252) < Decimal("1e-10")


# ── VolTargetSizer ────────────────────────────────────────────────────────────


def _make_sizer(
    annual_vol_pct: float,
    target_vol: str = "0.10",
) -> VolTargetSizer:
    """Create a sizer pre-loaded with a vol estimate near `annual_vol_pct`."""
    est = EWMAVolEstimator()
    # Feed in a steady return that produces roughly the desired vol
    daily_return = Decimal(str(annual_vol_pct)) / Decimal("252").sqrt()
    for _ in range(200):
        est.update(daily_return)
    return VolTargetSizer(est, target_annual_vol=Decimal(target_vol))


def test_target_leverage_at_target_vol_is_approx_1x() -> None:
    sizer = _make_sizer(annual_vol_pct=0.10)  # vol ≈ target → leverage ≈ 1×
    lev = sizer.target_leverage()
    assert Decimal("0.90") <= lev <= Decimal("1.10")


def test_target_leverage_high_vol_is_below_1x() -> None:
    sizer = _make_sizer(annual_vol_pct=0.20)  # vol = 2× target → leverage ≈ 0.5×
    lev = sizer.target_leverage()
    assert lev < Decimal("1.0")


def test_target_leverage_low_vol_uses_floor() -> None:
    # Even with tiny returns, annual_vol >= 8%, so leverage = 10%/8% = 1.25
    est = EWMAVolEstimator()
    sizer = VolTargetSizer(est, target_annual_vol=Decimal("0.10"))
    lev = sizer.target_leverage()
    # With zero vol, floor = 8%, leverage = 10%/8% = 1.25, capped at 1.75
    assert lev == Decimal("0.10") / VOL_FLOOR_ANNUAL


def test_target_leverage_capped_at_175() -> None:
    est = EWMAVolEstimator()
    # No data → vol at floor = 8%; target = 20% → raw leverage = 2.5× → capped
    sizer = VolTargetSizer(est, target_annual_vol=Decimal("0.20"))
    lev = sizer.target_leverage()
    assert lev == VOL_TARGET_CAP_LEVERAGE  # 1.75


def test_day_over_day_change_capped_at_10pct() -> None:
    sizer = _make_sizer(annual_vol_pct=0.10)
    lev1 = sizer.target_leverage()  # establishes prev_leverage

    # Now feed in a huge vol shock to create a large target change
    for _ in range(5):
        sizer._estimator.update(Decimal("0.20"))  # spike vol massively

    lev2 = sizer.target_leverage()
    # Change from lev1 should be capped at ±10%
    assert lev2 >= lev1 * Decimal("0.90")
    assert lev2 <= lev1 * Decimal("1.10")


def test_final_leverage_takes_minimum_of_vol_target_and_max_gross() -> None:
    est = EWMAVolEstimator()
    sizer = VolTargetSizer(est, target_annual_vol=Decimal("0.10"))
    # vol at floor 8% → target_leverage = 1.25
    vol_lev = sizer.target_leverage()
    assert vol_lev == Decimal("0.10") / VOL_FLOOR_ANNUAL

    # If max_gross is 1.0, final leverage must be capped at 1.0
    sizer2 = VolTargetSizer(EWMAVolEstimator(), target_annual_vol=Decimal("0.10"))
    final = sizer2.final_leverage(max_gross=Decimal("1.0"))
    assert final == Decimal("1.0")


# ── effective_max_gross ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (AgentId.HAIKU,  Decimal("1.50")),
        (AgentId.SONNET, Decimal("1.25")),
        (AgentId.OPUS,   Decimal("1.00")),
    ],
)
def test_base_caps_at_normal_conditions(agent: AgentId, expected: Decimal) -> None:
    result = effective_max_gross(
        agent,
        master_capability=DEFAULT_MASTER_CAPABILITY,
        vix_bucket=VixBucket.SWEET_SPOT,
        drawdown_bucket=DrawdownBucket.NORMAL,
    )
    assert result == expected


def test_vix_crisis_applies_025_scalar() -> None:
    result = effective_max_gross(
        AgentId.HAIKU,
        master_capability=Decimal("1.0"),
        vix_bucket=VixBucket.CRISIS,
        drawdown_bucket=DrawdownBucket.NORMAL,
    )
    # 1.50 × 1.0 × 0.25 × 1.0 = 0.375
    assert result == Decimal("1.50") * Decimal("0.25")


def test_drawdown_forced_cash_gives_zero() -> None:
    result = effective_max_gross(
        AgentId.SONNET,
        master_capability=Decimal("1.0"),
        vix_bucket=VixBucket.SWEET_SPOT,
        drawdown_bucket=DrawdownBucket.FORCED_CASH,
    )
    assert result == Decimal("0")


def test_master_capability_halved() -> None:
    result = effective_max_gross(
        AgentId.HAIKU,
        master_capability=Decimal("0.5"),
        vix_bucket=VixBucket.SWEET_SPOT,
        drawdown_bucket=DrawdownBucket.NORMAL,
    )
    assert result == Decimal("1.50") * Decimal("0.5")


def test_master_capability_above_15_raises() -> None:
    with pytest.raises(ValueError, match="MASTER_CAPABILITY"):
        effective_max_gross(
            AgentId.HAIKU,
            master_capability=Decimal("1.51"),
            vix_bucket=VixBucket.SWEET_SPOT,
            drawdown_bucket=DrawdownBucket.NORMAL,
        )


def test_master_capability_exactly_15_is_allowed() -> None:
    result = effective_max_gross(
        AgentId.HAIKU,
        master_capability=Decimal("1.5"),
        vix_bucket=VixBucket.SWEET_SPOT,
        drawdown_bucket=DrawdownBucket.NORMAL,
    )
    assert result == Decimal("1.50") * Decimal("1.5")


def test_combined_scalars() -> None:
    # Sonnet, MC=0.75, STRESS (0.5×), YELLOW (0.75×)
    # 1.25 × 0.75 × 0.5 × 0.75 = 0.3515625
    result = effective_max_gross(
        AgentId.SONNET,
        master_capability=Decimal("0.75"),
        vix_bucket=VixBucket.STRESS,
        drawdown_bucket=DrawdownBucket.YELLOW,
    )
    expected = Decimal("1.25") * Decimal("0.75") * Decimal("0.5") * Decimal("0.75")
    assert result == expected


# ── classify_vix ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("vix", "expected"),
    [
        ("10", VixBucket.VERY_LOW),
        ("11.9", VixBucket.VERY_LOW),
        ("12", VixBucket.SWEET_SPOT),
        ("17.9", VixBucket.SWEET_SPOT),
        ("18", VixBucket.ELEVATED),
        ("24.9", VixBucket.ELEVATED),
        ("25", VixBucket.STRESS),
        ("34.9", VixBucket.STRESS),
        ("35", VixBucket.CRISIS),
        ("80", VixBucket.CRISIS),
    ],
)
def test_classify_vix(vix: str, expected: VixBucket) -> None:
    assert classify_vix(Decimal(vix)) == expected


# CL-1 gate moved to tests/test_audit_sizing_gate.py (auditor-owned — builder must not edit).
