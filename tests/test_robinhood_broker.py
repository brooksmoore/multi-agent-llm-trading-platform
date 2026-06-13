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
    # No MCP client → still dry-run, never sends.
    rb = RobinhoodBroker(mcp_client=None, live_trading_enabled=False)
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


# ── live-shape parsing (offline, via fake MCP client) ─────────────────────────

class _FakeMcp:
    """Canned MCP responses keyed by tool name, mirroring real RH `data` envelopes."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return self._responses[name]

    def close(self) -> None:
        pass


def _live_broker(responses: dict) -> RobinhoodBroker:
    return RobinhoodBroker(mcp_client=_FakeMcp(responses), live_trading_enabled=True)


def test_get_account_reads_balances_from_get_portfolio() -> None:
    # Balances live under data; buying_power is a nested object. (get_accounts
    # carries NONE of these — sourcing them from there returned zeros.)
    rb = _live_broker({
        "get_portfolio": {
            "data": {
                "total_value": "1523.44",
                "cash": "200.10",
                "buying_power": {"buying_power": "200.1000", "display_currency": "USD"},
                "crypto_value": "100.00",
            },
            "guide": "...",
        }
    })
    acct = rb.get_account()
    assert acct.equity == Decimal("1523.44")
    assert acct.cash == Decimal("200.10")
    assert acct.buying_power == Decimal("200.1000")
    # cash account → no PDT concept
    assert acct.pattern_day_trader is False
    assert acct.daytrade_count == 0


def test_list_positions_parses_data_envelope() -> None:
    rb = _live_broker({
        "get_equity_positions": {
            "data": {"positions": [
                {"symbol": "AAPL", "quantity": "5", "average_buy_price": "180.0"},
                {"symbol": "BTC-USD", "quantity": "0.01", "average_buy_price": "60000"},
            ]},
            "guide": "...",
        }
    })
    pos = rb.list_positions()
    assert [p.symbol for p in pos] == ["AAPL", "BTC-USD"]
    assert pos[0].qty == Decimal("5")
    assert pos[1].asset_class.name == "CRYPTO"


def test_get_order_parses_data_envelope_and_cumulative_quantity() -> None:
    # filled qty is `cumulative_quantity`, NOT `filled_quantity` — the reconciler
    # keys fills off this; reading the wrong field hides every fill.
    rb = _live_broker({
        "get_equity_orders": {
            "data": {"orders": [{
                "id": "ord-1",
                "ref_id": str(new_id()),
                "symbol": "NVDA",
                "side": "buy",
                "quantity": "10",
                "cumulative_quantity": "7",
                "average_price": "120.0",
                "state": "partially_filled",
                "last_transaction_at": "2026-06-13T15:00:00Z",
            }]},
            "guide": "...",
        }
    })
    status = rb.get_order("ord-1")
    assert status.filled_qty == Decimal("7")
    assert status.state == BrokerOrderState.PARTIALLY_FILLED
    assert status.avg_fill_price == Decimal("120.0")


def test_find_order_by_client_id_matches_ref_id_in_envelope() -> None:
    cid = new_id()
    rb = _live_broker({
        "get_equity_orders": {
            "data": {"orders": [
                {"id": "ord-x", "ref_id": "00000000-0000-0000-0000-000000000000",
                 "symbol": "SPY", "side": "buy", "state": "filled"},
                {"id": "ord-y", "ref_id": str(cid),
                 "symbol": "QQQ", "side": "sell", "state": "filled"},
            ]},
            "guide": "...",
        }
    })
    found = rb.find_order_by_client_id(cid)
    assert found is not None
    assert found.broker_order_id == "ord-y"
    assert found.symbol == "QQQ"
