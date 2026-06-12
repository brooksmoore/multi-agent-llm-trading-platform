"""Offline tests for RobinhoodBroker — no network. Cover the safety gate,
idempotency, and type translation. Live MCP calls are not exercised here."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.types import (
    AgentId,
    Order,
    OrderClass,
    OrderSide,
    OrderState,
    OrderType,
    TimeInForce,
    new_id,
)
from execution.broker import Broker, BrokerOrderState
from execution.robinhood_broker import _RH_STATE_MAP, RobinhoodBroker


def _order(side: OrderSide = OrderSide.BUY, symbol: str = "AAPL") -> Order:
    return Order(
        id=new_id(),
        intent_id=new_id(),
        agent_id=AgentId.SONNET,
        symbol=symbol,
        side=side,
        qty=Decimal("3"),
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.DAY,
        state=OrderState.PENDING,
        created_at=datetime.now(UTC),
    )


def test_satisfies_broker_protocol() -> None:
    rb = RobinhoodBroker(live_trading_enabled=False)
    assert isinstance(rb, Broker)


def test_dry_run_does_not_send_and_returns_synthetic_id() -> None:
    rb = RobinhoodBroker(live_trading_enabled=False)
    bid = rb.submit_order(_order())
    assert bid.startswith("DRYRUN-")


def test_dry_run_default_even_with_no_token() -> None:
    # No token → still dry-run, never sends.
    rb = RobinhoodBroker(token_provider=None, live_trading_enabled=False)
    bid = rb.submit_order(_order())
    assert bid.startswith("DRYRUN-")


def test_submit_is_idempotent_on_order_id() -> None:
    rb = RobinhoodBroker(live_trading_enabled=False)
    o = _order()
    first = rb.submit_order(o)
    second = rb.submit_order(o)
    assert first == second


def test_build_order_args_translation() -> None:
    rb = RobinhoodBroker(live_trading_enabled=False)
    o = _order(side=OrderSide.SELL, symbol="NVDA")
    args = rb._build_order_args(o)
    assert args["symbol"] == "NVDA"
    assert args["side"] == "sell"
    assert args["quantity"] == "3"
    assert args["type"] == "market"
    assert args["ref_id"] == str(o.id)
    assert args["account_number"] == "981398050"


def test_get_account_dry_run_is_neutral() -> None:
    rb = RobinhoodBroker(live_trading_enabled=False)
    acct = rb.get_account()
    assert acct.equity == Decimal("0")
    assert acct.pattern_day_trader is False


def test_list_positions_dry_run_raises_unavailable() -> None:
    import pytest
    from execution.broker import BrokerUnavailable
    rb = RobinhoodBroker(live_trading_enabled=False)
    with pytest.raises(BrokerUnavailable):
        rb.list_positions()


def test_translate_order_state_mapping() -> None:
    rb = RobinhoodBroker(live_trading_enabled=False)
    status = rb._translate_order({
        "id": "abc123",
        "client_order_id": str(new_id()),
        "symbol": "AAPL",
        "side": "buy",
        "quantity": "3",
        "filled_quantity": "3",
        "average_price": "190.5",
        "state": "filled",
    })
    assert status.state == BrokerOrderState.FILLED
    assert status.filled_qty == Decimal("3")
    assert status.avg_fill_price == Decimal("190.5")


def test_unknown_state_falls_back() -> None:
    rb = RobinhoodBroker(live_trading_enabled=False)
    status = rb._translate_order({"id": "x", "status": "some_new_rh_state"})
    assert status.state == BrokerOrderState.UNKNOWN


def test_state_map_covers_common_terminal_states() -> None:
    for k in ("filled", "canceled", "rejected", "partially_filled"):
        assert k in _RH_STATE_MAP
