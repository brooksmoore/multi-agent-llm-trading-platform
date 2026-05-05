"""Tests for execution/alpaca_broker.py — AlpacaBroker adapter.

Uses unittest.mock to patch TradingClient so no real Alpaca connection is needed.
All tests exercise translation logic, error mapping, and idempotency behaviour.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from alpaca.common.exceptions import APIError

import alpaca.trading.enums as alpaca_enums

from core.types import AgentId, AssetClass, OrderSide, OrderType, TimeInForce
from execution.alpaca_broker import AlpacaBroker, _to_alpaca_tif
from execution.broker import BrokerOrderState, BrokerRejection, BrokerUnavailable
from execution.fake_broker import make_market_order

# ── Helpers ────────────────────────────────────────────────────────────────────

_TS = datetime(2026, 4, 24, 10, 0, tzinfo=UTC)
_BROKER_UUID = str(uuid4())
_CLIENT_UUID = str(uuid4())


def _make_alpaca_order(
    *,
    broker_id: str = _BROKER_UUID,
    client_id: str = _CLIENT_UUID,
    symbol: str = "SPY",
    side: str = "buy",
    qty: str = "10",
    filled_qty: str = "10",
    filled_avg_price: str | None = "450.00",
    status: str = "filled",
    submitted_at: datetime = _TS,
    updated_at: datetime = _TS,
) -> MagicMock:
    """Build a mock AlpacaOrder with the fields AlpacaBroker reads."""
    mo = MagicMock()
    mo.id = UUID(broker_id)
    mo.client_order_id = client_id
    mo.symbol = symbol
    mo.side = MagicMock(value=side)
    mo.qty = Decimal(qty)
    mo.filled_qty = Decimal(filled_qty)
    mo.filled_avg_price = Decimal(filled_avg_price) if filled_avg_price else None
    mo.status = MagicMock(value=status)
    mo.submitted_at = submitted_at
    mo.updated_at = updated_at
    return mo


def _make_alpaca_position(
    *,
    symbol: str = "SPY",
    qty: str = "10",
    side: str = "long",
    avg_entry: str = "440.00",
    current: str = "450.00",
    asset_class: str = "us_equity",
) -> MagicMock:
    mp = MagicMock()
    mp.symbol = symbol
    mp.qty = Decimal(qty)
    mp.side = MagicMock(value=side)
    mp.avg_entry_price = Decimal(avg_entry)
    mp.current_price = Decimal(current)
    mp.asset_class = MagicMock(value=asset_class)
    return mp


def _make_alpaca_account(
    *,
    cash: str = "5000.00",
    equity: str = "10000.00",
    buying_power: str = "20000.00",
    pdt: bool = False,
    daytrade_count: int = 0,
) -> MagicMock:
    ma = MagicMock()
    ma.cash = Decimal(cash)
    ma.equity = Decimal(equity)
    ma.buying_power = Decimal(buying_power)
    ma.pattern_day_trader = pdt
    ma.daytrade_count = daytrade_count
    return ma


def _broker_with_mock_client() -> tuple[AlpacaBroker, MagicMock]:
    """Return an AlpacaBroker whose TradingClient is a MagicMock."""
    with patch("execution.alpaca_broker.TradingClient") as mock_class:
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        broker = AlpacaBroker(api_key="test-key", secret_key="test-secret", paper=True)
        return broker, mock_client


# ── submit_order ───────────────────────────────────────────────────────────────


def test_submit_market_order_returns_broker_id() -> None:
    broker, client = _broker_with_mock_client()
    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU
    )
    alpaca_order = _make_alpaca_order(
        broker_id=_BROKER_UUID,
        client_id=str(order.id),
        status="accepted",
        filled_qty="0",
        filled_avg_price=None,
    )
    client.submit_order.return_value = alpaca_order

    broker_id = broker.submit_order(order)
    assert broker_id == _BROKER_UUID
    client.submit_order.assert_called_once()


def test_submit_limit_order_passes_limit_price() -> None:
    broker, client = _broker_with_mock_client()
    order = make_market_order(
        symbol="AAPL", side=OrderSide.BUY, qty=Decimal("5"), agent_id=AgentId.HAIKU
    )
    limit_order = dc_replace(
        order,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("185.00"),
    )
    client.submit_order.return_value = _make_alpaca_order(
        broker_id=_BROKER_UUID, client_id=str(limit_order.id), status="new",
        filled_qty="0", filled_avg_price=None,
    )
    broker.submit_order(limit_order)

    call_args = client.submit_order.call_args[0][0]
    assert float(call_args.limit_price) == pytest.approx(185.0)


def test_submit_unsupported_order_type_raises_rejection() -> None:
    broker, client = _broker_with_mock_client()
    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU
    )
    stop_order = dc_replace(order, order_type=OrderType.STOP, stop_price=Decimal("440.00"))
    with pytest.raises(BrokerRejection):
        broker.submit_order(stop_order)


def test_submit_limit_missing_price_raises_rejection() -> None:
    broker, client = _broker_with_mock_client()
    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU
    )
    limit_no_price = dc_replace(order, order_type=OrderType.LIMIT, limit_price=None)
    with pytest.raises(BrokerRejection):
        broker.submit_order(limit_no_price)


# ── Error mapping ──────────────────────────────────────────────────────────────


def test_api_4xx_raises_broker_rejection() -> None:
    broker, client = _broker_with_mock_client()
    exc = APIError('{"code":40010001,"message":"account is not authorized"}')
    exc._http_error = MagicMock()
    exc._http_error.response = MagicMock(status_code=403)
    client.submit_order.side_effect = exc

    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU
    )
    with pytest.raises(BrokerRejection):
        broker.submit_order(order)


def test_api_5xx_raises_broker_unavailable() -> None:
    broker, client = _broker_with_mock_client()
    exc = APIError('{"code":50000000,"message":"internal server error"}')
    exc._http_error = MagicMock()
    exc._http_error.response = MagicMock(status_code=500)
    client.submit_order.side_effect = exc

    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU
    )
    with pytest.raises(BrokerUnavailable):
        broker.submit_order(order)


def test_duplicate_client_order_id_returns_existing_broker_id() -> None:
    """Alpaca returns 422 for duplicate client_order_id; broker falls back to find_order."""
    broker, client = _broker_with_mock_client()

    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU
    )
    existing_alpaca_order = _make_alpaca_order(
        broker_id=_BROKER_UUID, client_id=str(order.id), status="accepted",
        filled_qty="0", filled_avg_price=None,
    )

    dup_exc = APIError('{"code":42210000,"message":"client_order_id must be unique"}')
    dup_exc._http_error = MagicMock()
    dup_exc._http_error.response = MagicMock(status_code=422)
    client.submit_order.side_effect = dup_exc
    client.get_order_by_client_id.return_value = existing_alpaca_order

    broker_id = broker.submit_order(order)
    assert broker_id == _BROKER_UUID


# ── cancel_order ───────────────────────────────────────────────────────────────


def test_cancel_order_calls_alpaca() -> None:
    broker, client = _broker_with_mock_client()
    broker.cancel_order(_BROKER_UUID)
    client.cancel_order_by_id.assert_called_once_with(_BROKER_UUID)


def test_cancel_order_404_raises_rejection() -> None:
    broker, client = _broker_with_mock_client()
    exc = APIError('{"code":40410000,"message":"order not found"}')
    exc._http_error = MagicMock()
    exc._http_error.response = MagicMock(status_code=404)
    client.cancel_order_by_id.side_effect = exc
    with pytest.raises(BrokerRejection):
        broker.cancel_order(_BROKER_UUID)


# ── get_order ──────────────────────────────────────────────────────────────────


def test_get_order_translates_status() -> None:
    broker, client = _broker_with_mock_client()
    client.get_order_by_id.return_value = _make_alpaca_order(status="filled")
    status = broker.get_order(_BROKER_UUID)
    assert status.state == BrokerOrderState.FILLED
    assert status.filled_qty == Decimal("10")
    assert status.avg_fill_price == Decimal("450.00")


def test_get_order_partially_filled() -> None:
    broker, client = _broker_with_mock_client()
    client.get_order_by_id.return_value = _make_alpaca_order(
        status="partially_filled", filled_qty="5", qty="10", filled_avg_price="448.00",
    )
    status = broker.get_order(_BROKER_UUID)
    assert status.state == BrokerOrderState.PARTIALLY_FILLED
    assert status.filled_qty == Decimal("5")


def test_get_order_not_found_raises_rejection() -> None:
    broker, client = _broker_with_mock_client()
    exc = APIError('{"code":40410000,"message":"order not found"}')
    exc._http_error = MagicMock()
    exc._http_error.response = MagicMock(status_code=404)
    client.get_order_by_id.side_effect = exc
    with pytest.raises(BrokerRejection):
        broker.get_order(_BROKER_UUID)


# ── find_order_by_client_id ────────────────────────────────────────────────────


def test_find_order_by_client_id_found() -> None:
    broker, client = _broker_with_mock_client()
    client_id = uuid4()
    client.get_order_by_client_id.return_value = _make_alpaca_order(
        client_id=str(client_id), status="accepted", filled_qty="0", filled_avg_price=None,
    )
    result = broker.find_order_by_client_id(client_id)
    assert result is not None
    assert result.state == BrokerOrderState.ACCEPTED


def test_find_order_by_client_id_not_found_returns_none() -> None:
    broker, client = _broker_with_mock_client()
    exc = APIError('{"code":40410000,"message":"order not found"}')
    exc._http_error = MagicMock()
    exc._http_error.response = MagicMock(status_code=404)
    client.get_order_by_client_id.side_effect = exc
    result = broker.find_order_by_client_id(uuid4())
    assert result is None


# ── list_positions ─────────────────────────────────────────────────────────────


def test_list_positions_returns_translated_positions() -> None:
    broker, client = _broker_with_mock_client()
    client.get_all_positions.return_value = [
        _make_alpaca_position(symbol="SPY", qty="10", side="long"),
        _make_alpaca_position(symbol="QQQ", qty="5", side="long", avg_entry="380.00"),
    ]
    positions = broker.list_positions()
    assert len(positions) == 2
    symbols = {p.symbol for p in positions}
    assert symbols == {"SPY", "QQQ"}


def test_list_positions_short_is_negative_qty() -> None:
    broker, client = _broker_with_mock_client()
    client.get_all_positions.return_value = [
        _make_alpaca_position(symbol="SPY", qty="10", side="short"),
    ]
    positions = broker.list_positions()
    assert positions[0].qty == Decimal("-10")


def test_list_positions_crypto_asset_class() -> None:
    broker, client = _broker_with_mock_client()
    client.get_all_positions.return_value = [
        _make_alpaca_position(symbol="BTCUSD", qty="0.5", asset_class="crypto"),
    ]
    positions = broker.list_positions()
    assert positions[0].asset_class == AssetClass.CRYPTO


# ── get_account ────────────────────────────────────────────────────────────────


def test_get_account_returns_translated_account() -> None:
    broker, client = _broker_with_mock_client()
    client.get_account.return_value = _make_alpaca_account(
        cash="3000.00", equity="9000.00", buying_power="18000.00", pdt=True, daytrade_count=2,
    )
    acct = broker.get_account()
    assert acct.cash == Decimal("3000.00")
    assert acct.equity == Decimal("9000.00")
    assert acct.buying_power == Decimal("18000.00")
    assert acct.pattern_day_trader is True
    assert acct.daytrade_count == 2


# ── register_event_callback ────────────────────────────────────────────────────


def test_register_callback_stored() -> None:
    broker, _ = _broker_with_mock_client()
    cb = MagicMock()
    broker.register_event_callback(cb)
    assert broker._callback is cb


# ── _to_alpaca_tif — crypto TIF override (regression: code 42210000) ──────────
# Alpaca rejects DAY time-in-force for crypto orders with error code 42210000.
# These tests guard the DAY→GTC override so it can never regress silently.


@pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSD", "SOLUSD"])
def test_crypto_day_tif_converted_to_gtc(symbol: str) -> None:
    """Crypto symbols with TIF=DAY must be converted to GTC before submission."""
    result = _to_alpaca_tif(TimeInForce.DAY.value, symbol)
    assert result == alpaca_enums.TimeInForce.GTC, (
        f"{symbol} with TIF=DAY must map to GTC (Alpaca rejects DAY for crypto)"
    )


@pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSD", "SOLUSD"])
def test_crypto_gtc_tif_unchanged(symbol: str) -> None:
    """Crypto orders already using GTC must not be double-converted."""
    result = _to_alpaca_tif(TimeInForce.GTC.value, symbol)
    assert result == alpaca_enums.TimeInForce.GTC


@pytest.mark.parametrize("symbol", ["SPY", "QQQ", "AAPL", "IWM", "GLD"])
def test_equity_day_tif_unchanged(symbol: str) -> None:
    """Equity symbols must keep DAY as-is — the override must not bleed over."""
    result = _to_alpaca_tif(TimeInForce.DAY.value, symbol)
    assert result == alpaca_enums.TimeInForce.DAY


def test_slash_notation_crypto_converted(symbol: str = "BTC/USD") -> None:
    """Slash-notation crypto symbols (alternative Alpaca format) also get GTC."""
    result = _to_alpaca_tif(TimeInForce.DAY.value, symbol)
    assert result == alpaca_enums.TimeInForce.GTC
