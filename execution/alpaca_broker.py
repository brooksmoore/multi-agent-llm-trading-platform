"""AlpacaBroker — production Broker adapter wrapping alpaca-py.

Implements the Broker Protocol from broker.py using:
  - TradingClient  (sync REST)  for orders, positions, account
  - TradingStream  (async WS)   for real-time fill/cancel callbacks

Usage:
    broker = AlpacaBroker(api_key=..., secret_key=..., paper=True)
    broker.register_event_callback(oms._on_broker_event)
    broker.start_stream()   # optional; reconciler handles polling fallback

Testing: mock `TradingClient` via `unittest.mock.patch` — no real Alpaca needed.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import alpaca.trading.enums as alpaca_enums
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.stream import TradingStream
from requests.exceptions import RequestException

from core.types import (
    AgentId,
    AssetClass,
    Fill,
    Order,
    OrderId,
    OrderSide,
    OrderType,
    new_id,
)
from core.types import (
    is_crypto_symbol as _is_crypto_symbol,
)
from execution.broker import (
    BrokerAccount,
    BrokerEventCallback,
    BrokerOrderEvent,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerRejection,
    BrokerUnavailable,
)

logger = logging.getLogger(__name__)

# ── Status translation ────────────────────────────────────────────────────────

_ALPACA_STATUS_TO_BROKER: dict[str, BrokerOrderState] = {
    "new":                   BrokerOrderState.NEW,
    "pending_new":           BrokerOrderState.NEW,
    "held":                  BrokerOrderState.NEW,
    "accepted":              BrokerOrderState.ACCEPTED,
    "accepted_for_bidding":  BrokerOrderState.ACCEPTED,
    "pending_cancel":        BrokerOrderState.ACCEPTED,
    "pending_replace":       BrokerOrderState.ACCEPTED,
    "calculated":            BrokerOrderState.ACCEPTED,
    "stopped":               BrokerOrderState.ACCEPTED,
    "partially_filled":      BrokerOrderState.PARTIALLY_FILLED,
    "filled":                BrokerOrderState.FILLED,
    "done_for_day":          BrokerOrderState.EXPIRED,
    "canceled":              BrokerOrderState.CANCELED,
    "replaced":              BrokerOrderState.CANCELED,
    "expired":               BrokerOrderState.EXPIRED,
    "rejected":              BrokerOrderState.REJECTED,
    "suspended":             BrokerOrderState.REJECTED,
}

_ALPACA_TRADE_EVENT_TO_STATE: dict[str, BrokerOrderState] = {
    "new":             BrokerOrderState.NEW,
    "pending_new":     BrokerOrderState.NEW,
    "accepted":        BrokerOrderState.ACCEPTED,
    "partial_fill":    BrokerOrderState.PARTIALLY_FILLED,
    "fill":            BrokerOrderState.FILLED,
    "canceled":        BrokerOrderState.CANCELED,
    "expired":         BrokerOrderState.EXPIRED,
    "rejected":        BrokerOrderState.REJECTED,
    "replaced":        BrokerOrderState.CANCELED,
    "pending_cancel":  BrokerOrderState.ACCEPTED,
    "pending_replace": BrokerOrderState.ACCEPTED,
    "restated":        BrokerOrderState.ACCEPTED,
}

CRYPTO_TAKER_FEE: Decimal = Decimal("0.0025")

_ALPACA_ASSET_CLASS_TO_OURS: dict[str, AssetClass] = {
    "us_equity":  AssetClass.EQUITY,
    "us_option":  AssetClass.OPTION,
    "crypto":     AssetClass.CRYPTO,
    "crypto_perp": AssetClass.CRYPTO,
}


def _to_alpaca_side(side: OrderSide) -> alpaca_enums.OrderSide:
    return alpaca_enums.OrderSide(side.value)


# TIF=DAY is rejected for crypto (only GTC/IOC accepted), so we override
# DAY → GTC at submit time. Crypto detection lives in core/types.py.


def _to_alpaca_tif(tif: str, symbol: str) -> alpaca_enums.TimeInForce:
    if _is_crypto_symbol(symbol) and tif == "day":
        return alpaca_enums.TimeInForce.GTC
    return alpaca_enums.TimeInForce(tif)


def _decimal(v: Any) -> Decimal:  # noqa: ANN401
    """Convert alpaca's Decimal/str/None to Decimal safely."""
    if v is None:
        return Decimal("0")
    return Decimal(str(v))


# ── AlpacaBroker ─────────────────────────────────────────────────────────────


class AlpacaBroker:
    """Production Broker adapter — wraps alpaca-py.

    Idempotency note: Alpaca rejects duplicate client_order_id with HTTP 422.
    The `submit_order` method catches that and looks up the existing order,
    honoring the Broker Protocol's idempotency contract.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._client = TradingClient(api_key, secret_key, paper=paper)
        self._callback: BrokerEventCallback | None = None
        self._stream: TradingStream | None = None
        self._stream_thread: threading.Thread | None = None
        # client_order_id (str) → (agent_id, order_id, symbol, side)
        self._order_meta: dict[str, tuple[AgentId, OrderId, str, OrderSide]] = {}
        self._meta_lock = threading.Lock()

    # ── Broker Protocol ───────────────────────────────────────────────────────

    def submit_order(self, order: Order) -> str:
        """Submit order to Alpaca. Returns Alpaca's broker_order_id (UUID string)."""
        client_id = str(order.id)

        with self._meta_lock:
            self._order_meta[client_id] = (
                order.agent_id, order.id, order.symbol, order.side
            )

        side = _to_alpaca_side(order.side)
        tif = _to_alpaca_tif(order.time_in_force.value, order.symbol)

        try:
            if order.order_type == OrderType.MARKET:
                req: MarketOrderRequest | LimitOrderRequest = MarketOrderRequest(
                    symbol=order.symbol,
                    qty=float(order.qty),
                    side=side,
                    time_in_force=tif,
                    client_order_id=client_id,
                )
            elif order.order_type == OrderType.LIMIT:
                if order.limit_price is None:
                    raise BrokerRejection("LIMIT order missing limit_price")
                req = LimitOrderRequest(
                    symbol=order.symbol,
                    qty=float(order.qty),
                    side=side,
                    time_in_force=tif,
                    client_order_id=client_id,
                    limit_price=float(order.limit_price),
                )
            else:
                raise BrokerRejection(
                    f"AlpacaBroker: order_type {order.order_type!r} not yet supported"
                )

            result = cast("AlpacaOrder", self._client.submit_order(req))
            return str(result.id)

        except APIError as exc:
            # Idempotency: Alpaca rejects duplicate client_order_id with 422.
            # Fall back to find_order_by_client_id to return the existing broker id.
            if exc.status_code == 422:
                existing = self.find_order_by_client_id(order.id)
                if existing is not None:
                    return existing.broker_order_id
            if exc.status_code in (400, 403, 404, 422):
                raise BrokerRejection(str(exc)) from exc
            raise BrokerUnavailable(str(exc)) from exc
        except RequestException as exc:
            raise BrokerUnavailable(f"network error: {exc}") from exc

    def cancel_order(self, broker_order_id: str) -> None:
        try:
            self._client.cancel_order_by_id(broker_order_id)
        except APIError as exc:
            if exc.status_code in (400, 403, 404, 422):
                raise BrokerRejection(str(exc)) from exc
            raise BrokerUnavailable(str(exc)) from exc
        except RequestException as exc:
            raise BrokerUnavailable(f"network error: {exc}") from exc

    def get_order(self, broker_order_id: str) -> BrokerOrderStatus:
        try:
            ao = cast("AlpacaOrder", self._client.get_order_by_id(broker_order_id))
            return self._translate_order(ao)
        except APIError as exc:
            if exc.status_code == 404:
                raise BrokerRejection(f"Order {broker_order_id} not found") from exc
            raise BrokerUnavailable(str(exc)) from exc
        except RequestException as exc:
            raise BrokerUnavailable(f"network error: {exc}") from exc

    def find_order_by_client_id(self, client_order_id: OrderId) -> BrokerOrderStatus | None:
        try:
            ao = cast("AlpacaOrder", self._client.get_order_by_client_id(str(client_order_id)))
            return self._translate_order(ao)
        except APIError as exc:
            if exc.status_code == 404:
                return None
            raise BrokerUnavailable(str(exc)) from exc
        except RequestException as exc:
            raise BrokerUnavailable(f"network error: {exc}") from exc

    def list_positions(self) -> list[BrokerPosition]:
        try:
            positions = cast("list[AlpacaPosition]", self._client.get_all_positions())
        except APIError as exc:
            raise BrokerUnavailable(str(exc)) from exc
        except RequestException as exc:
            raise BrokerUnavailable(f"network error: {exc}") from exc
        return [self._translate_position(p) for p in positions]

    def get_account(self) -> BrokerAccount:
        try:
            acct = cast("TradeAccount", self._client.get_account())
        except APIError as exc:
            raise BrokerUnavailable(str(exc)) from exc
        except RequestException as exc:
            raise BrokerUnavailable(f"network error: {exc}") from exc
        return BrokerAccount(
            cash=_decimal(acct.cash),
            equity=_decimal(acct.equity),
            buying_power=_decimal(acct.buying_power),
            pattern_day_trader=bool(acct.pattern_day_trader),
            daytrade_count=int(acct.daytrade_count or 0),
        )

    def register_event_callback(self, callback: BrokerEventCallback) -> None:
        self._callback = callback

    # ── Stream control ────────────────────────────────────────────────────────

    def start_stream(self) -> None:
        """Start the Alpaca trade-updates WebSocket in a background daemon thread."""
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return

        stream = TradingStream(self._api_key, self._secret_key, paper=self._paper)

        broker_ref = self  # capture self for use inside async closure

        @stream.subscribe_trade_updates
        async def _on_update(update: Any) -> None:  # noqa: ANN401
            cb = broker_ref._callback
            if cb is None:
                return
            event = broker_ref._trade_update_to_event(update)
            if event is not None:
                cb(event)

        self._stream = stream
        self._stream_thread = threading.Thread(
            target=stream.run,
            daemon=True,
            name="alpaca-trade-stream",
        )
        self._stream_thread.start()
        logger.info("AlpacaBroker: trade stream started (paper=%s)", self._paper)

    def stop_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream = None
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=5)
            self._stream_thread = None

    # ── Translators ───────────────────────────────────────────────────────────

    def _translate_order(self, ao: AlpacaOrder) -> BrokerOrderStatus:
        status_str = ao.status.value if ao.status else "unknown"
        state = _ALPACA_STATUS_TO_BROKER.get(status_str, BrokerOrderState.UNKNOWN)

        client_id_str = ao.client_order_id or ""
        try:
            client_order_id = UUID(client_id_str)
        except (ValueError, AttributeError):
            client_order_id = UUID(int=0)

        side = OrderSide(ao.side.value) if ao.side else OrderSide.BUY

        # Mirror the in-kind crypto fee adjustment from the stream path so the
        # reconciler's synthesized fills (when the stream missed an event) use
        # the same net qty the position endpoint actually shows.
        filled_qty = _decimal(ao.filled_qty)
        ac_str = ao.asset_class.value if ao.asset_class else ""
        if (
            _ALPACA_ASSET_CLASS_TO_OURS.get(ac_str) == AssetClass.CRYPTO
            and side == OrderSide.BUY
        ):
            filled_qty = filled_qty * (Decimal("1") - CRYPTO_TAKER_FEE)

        return BrokerOrderStatus(
            broker_order_id=str(ao.id),
            client_order_id=client_order_id,
            symbol=ao.symbol or "",
            side=side,
            qty=_decimal(ao.qty),
            filled_qty=filled_qty,
            avg_fill_price=_decimal(ao.filled_avg_price) if ao.filled_avg_price else None,
            state=state,
            submitted_at=ao.submitted_at or datetime.now(UTC),
            updated_at=ao.updated_at or datetime.now(UTC),
        )

    def _translate_position(self, ap: AlpacaPosition) -> BrokerPosition:
        asset_class_str = ap.asset_class.value if ap.asset_class else "us_equity"
        asset_class = _ALPACA_ASSET_CLASS_TO_OURS.get(asset_class_str, AssetClass.EQUITY)

        # Alpaca qty is always positive; side field tells long vs short.
        qty = _decimal(ap.qty)
        if ap.side and ap.side.value == "short":
            qty = -qty

        return BrokerPosition(
            symbol=ap.symbol or "",
            qty=qty,
            avg_entry_price=_decimal(ap.avg_entry_price),
            current_price=_decimal(ap.current_price),
            asset_class=asset_class,
        )

    def _trade_update_to_event(self, update: Any) -> BrokerOrderEvent | None:  # noqa: ANN401
        """Translate a TradeUpdate from TradingStream to a BrokerOrderEvent."""
        try:
            ao: AlpacaOrder = update.order
            event_str = update.event.value if hasattr(update.event, "value") else str(update.event)
            new_state = _ALPACA_TRADE_EVENT_TO_STATE.get(event_str, BrokerOrderState.UNKNOWN)

            client_id_str = ao.client_order_id or ""
            try:
                client_order_id = UUID(client_id_str)
            except (ValueError, AttributeError):
                logger.warning(
                    "AlpacaBroker: bad client_order_id in stream event: %r", client_id_str
                )
                return None

            ts = update.timestamp or datetime.now(UTC)

            fill: Fill | None = None
            if event_str in ("fill", "partial_fill") and update.price and update.qty:
                with self._meta_lock:
                    meta = self._order_meta.get(client_id_str)
                agent_id = meta[0] if meta else AgentId.HAIKU
                side = meta[3] if meta else OrderSide.BUY
                fill_qty = _decimal(update.qty)
                # Alpaca crypto fees are charged in-kind: a 0.25% taker fee is
                # deducted from the BASE asset on buys (we receive less crypto
                # than filled) and from the QUOTE asset on sells (we receive
                # less USD). Record the net qty for buys so OMS books match
                # the broker position. Sells need no qty adjustment — proceeds
                # are reduced in USD, which the broker already reflects.
                ac_str = ao.asset_class.value if ao.asset_class else ""
                if (
                    _ALPACA_ASSET_CLASS_TO_OURS.get(ac_str) == AssetClass.CRYPTO
                    and side == OrderSide.BUY
                ):
                    fill_qty = fill_qty * (Decimal("1") - CRYPTO_TAKER_FEE)
                fill = Fill(
                    id=new_id(),
                    order_id=client_order_id,
                    agent_id=agent_id,
                    symbol=ao.symbol or "",
                    side=side,
                    qty=fill_qty,
                    price=_decimal(update.price),
                    timestamp=ts,
                    is_partial=(event_str == "partial_fill"),
                )

            return BrokerOrderEvent(
                broker_order_id=str(ao.id),
                client_order_id=client_order_id,
                new_state=new_state,
                fill=fill,
                timestamp=ts,
            )
        except Exception:
            logger.exception("AlpacaBroker: failed to translate trade update")
            return None
