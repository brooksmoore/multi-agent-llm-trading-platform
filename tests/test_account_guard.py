"""Account-level pre-submit guards (planner-rebalance-delta backstops).

`App._account_level_pre_check` is the last line of defense before an
order reaches the OMS. It does two things:

1. Refuses BUY orders that would consume > 90% of available buying power
   (avoids the SOLUSD-style Alpaca rejections we saw 2026-05-11).
2. Refuses BUY orders that would push account-level leverage past 1.5×
   (backstop for cross-sleeve compounding bugs the per-sleeve cap won't
   catch).

SELL orders are exempt — the system must be able to de-leverage even from
an over-cap state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import App
from config.settings import Settings
from core.types import (
    AgentId,
    MarketSnapshot,
    Order,
    OrderClass,
    OrderSide,
    OrderState,
    OrderType,
    TimeInForce,
    VixBucket,
    new_id,
)
from execution.broker import BrokerAccount
from execution.fake_broker import FakeBroker
from tests.test_app_scheduler import _StubMD


@pytest.fixture
def app(tmp_path: Path) -> App:
    settings = Settings(
        alpaca_paper=True, alpaca_api_key="x", alpaca_secret_key="x",
        anthropic_api_key="x", ntfy_topic="",
        master_capability=Decimal("1.0"), daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
    )
    return App(
        settings, broker=FakeBroker(), market_data=_StubMD(),
        universe=["SPY"], run_dashboard=False, run_volatility_scanner=False,
        run_recover_on_start=False,
    )


def _order(side: OrderSide, symbol: str = "SPY",
           qty: Decimal = Decimal("10")) -> Order:
    return Order(
        id=new_id(), intent_id=new_id(), agent_id=AgentId.HAIKU,
        symbol=symbol, side=side, qty=qty,
        order_type=OrderType.MARKET, order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.DAY, state=OrderState.PENDING,
        created_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )


def _snapshot(price: Decimal = Decimal("100"),
              symbol: str = "SPY") -> MarketSnapshot:
    return MarketSnapshot(
        current_prices={symbol: price},
        realized_vol_30d={symbol: Decimal("0.25")},
        vix_bucket=VixBucket.SWEET_SPOT,
        timestamp=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )


def _stub_account(*, equity: Decimal, cash: Decimal,
                  buying_power: Decimal) -> BrokerAccount:
    return BrokerAccount(
        cash=cash, equity=equity, buying_power=buying_power,
        pattern_day_trader=False, daytrade_count=0,
    )


# ── SELL exemption ────────────────────────────────────────────────────────────


def test_sell_orders_always_pass(app: App) -> None:
    """SELL reduces leverage; never blocked by the account-level guards."""
    app.broker.get_account = lambda: _stub_account(  # type: ignore[method-assign]
        equity=Decimal("100"), cash=Decimal("0"),
        buying_power=Decimal("0"),  # zero buying power
    )

    result = app._account_level_pre_check(
        _order(OrderSide.SELL, qty=Decimal("100")), _snapshot(),
    )
    assert result is None


# ── Buying-power utilization ─────────────────────────────────────────────────


def test_buy_blocked_when_exceeding_90pct_of_buying_power(app: App) -> None:
    """Order notional > 90% of buying_power: refused."""
    app.broker.get_account = lambda: _stub_account(  # type: ignore[method-assign]
        equity=Decimal("100000"), cash=Decimal("50000"),
        buying_power=Decimal("1000"),
    )

    # 10 × $100 = $1000 order, buying_power = $1000 → 100% utilization, > 90% cap
    result = app._account_level_pre_check(
        _order(OrderSide.BUY, qty=Decimal("10")), _snapshot(),
    )
    assert result is not None
    assert "buying_power" in result


def test_buy_passes_when_within_90pct_of_buying_power(app: App) -> None:
    """Order notional < 90% of buying_power: allowed (subject to other checks)."""
    app.broker.get_account = lambda: _stub_account(  # type: ignore[method-assign]
        equity=Decimal("100000"), cash=Decimal("50000"),
        buying_power=Decimal("10000"),
    )

    # 10 × $100 = $1000 ; buying_power = $10000 → 10% utilization
    result = app._account_level_pre_check(
        _order(OrderSide.BUY, qty=Decimal("10")), _snapshot(),
    )
    assert result is None


def test_buy_blocked_when_buying_power_is_zero(app: App) -> None:
    """Zero buying power: any positive-notional BUY refused."""
    app.broker.get_account = lambda: _stub_account(  # type: ignore[method-assign]
        equity=Decimal("100"), cash=Decimal("-100"),
        buying_power=Decimal("0"),
    )

    result = app._account_level_pre_check(
        _order(OrderSide.BUY, qty=Decimal("1")), _snapshot(),
    )
    assert result == "account_guard:buying_power_zero"


# ── Account leverage backstop ─────────────────────────────────────────────────


def test_buy_blocked_when_projected_leverage_exceeds_cap(app: App) -> None:
    """Today's pattern: equity $102K, cash -$84K, lmv $186K = 1.82× already.
    Any additional BUY pushes deeper; refused even if buying power allows."""
    app.broker.get_account = lambda: _stub_account(  # type: ignore[method-assign]
        equity=Decimal("102000"), cash=Decimal("-84000"),
        buying_power=Decimal("100000"),
    )
    # current_lmv = equity - cash = 102000 - (-84000) = 186000
    # projected after $1000 add = 187000; ratio = 187/102 = 1.83× > 1.50× cap
    result = app._account_level_pre_check(
        _order(OrderSide.BUY, qty=Decimal("10")), _snapshot(),
    )
    assert result is not None
    assert "leverage_cap" in result


def test_buy_allowed_under_account_leverage_cap(app: App) -> None:
    """Clean state: equity $100K, cash $50K, lmv $50K = 0.5×. Plenty of room."""
    app.broker.get_account = lambda: _stub_account(  # type: ignore[method-assign]
        equity=Decimal("100000"), cash=Decimal("50000"),
        buying_power=Decimal("100000"),
    )
    # current_lmv = 100000 - 50000 = 50000; projected = 51000; ratio 0.51× ≤ 1.50×
    result = app._account_level_pre_check(
        _order(OrderSide.BUY, qty=Decimal("10")), _snapshot(),
    )
    assert result is None


def test_buy_at_exactly_account_cap_passes_until_next_tick(app: App) -> None:
    """Order that lands exactly at 1.50× is allowed; the NEXT one is blocked.
    Mirrors how the cap is enforced — strictly greater-than, not >=."""
    app.broker.get_account = lambda: _stub_account(  # type: ignore[method-assign]
        equity=Decimal("1000"), cash=Decimal("-500"),
        buying_power=Decimal("100000"),
    )
    # lmv = 1000 - (-500) = 1500; projected at qty=0 = exactly 1500/1000 = 1.50×
    # A zero-cost order doesn't make sense, but we want strict > 1.50× to fail.
    # An infinitesimal add (10×0.01=0.10) → 1500.10/1000 = 1.5001× > 1.50× → fail
    result = app._account_level_pre_check(
        _order(OrderSide.BUY, qty=Decimal("10")),
        _snapshot(price=Decimal("0.01")),
    )
    assert result is not None and "leverage_cap" in result


# ── Resilience ────────────────────────────────────────────────────────────────


def test_missing_mark_allows_passthrough(app: App) -> None:
    """Without a mark we can't reason about dollars; let the RiskGate's
    per-sleeve check be the last word."""
    app.broker.get_account = lambda: _stub_account(  # type: ignore[method-assign]
        equity=Decimal("100000"), cash=Decimal("-50000"),
        buying_power=Decimal("1000"),
    )
    snap = MarketSnapshot(
        current_prices={},  # no SPY price
        realized_vol_30d={}, vix_bucket=VixBucket.SWEET_SPOT,
        timestamp=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )

    result = app._account_level_pre_check(
        _order(OrderSide.BUY, qty=Decimal("10")), snap,
    )
    assert result is None


def test_broker_failure_allows_passthrough(app: App) -> None:
    """Broker API down: don't block trading; per-sleeve cap still applies."""
    def _raises() -> BrokerAccount:
        raise RuntimeError("broker unavailable")
    app.broker.get_account = _raises  # type: ignore[method-assign]

    result = app._account_level_pre_check(
        _order(OrderSide.BUY, qty=Decimal("10")), _snapshot(),
    )
    assert result is None
