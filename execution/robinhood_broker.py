"""RobinhoodBroker: Broker adapter for Robinhood's agentic trading MCP endpoint.

Wraps Robinhood's MCP server (`agent.robinhood.com/mcp/trading`) behind the same
`Broker` Protocol the OMS already uses for AlpacaBroker / FakeBroker. The OMS and
the rest of the system are unaware of the transport.

────────────────────────────────────────────────────────────────────────────────
⚠️  ONE THING YOU MUST KNOW BEFORE FUNDING THIS WITH REAL MONEY
────────────────────────────────────────────────────────────────────────────────
SAFETY GATE — `live_trading_enabled` defaults to False. In that state every
submit_order() is a DRY RUN: it logs the order it *would* have placed, returns
a synthetic `DRYRUN-<uuid>` broker id, and sends NOTHING to Robinhood. You must
explicitly pass live_trading_enabled=True (and a real auth token) to place live
orders. Arming this is the operator's decision, never made implicitly.

MCP schema verified via live probe 2026-06-10 (list_tools() + get_accounts call).
Tool names, argument shapes, and response field names below reflect confirmed reality.

Transport: a minimal synchronous MCP-over-HTTP (JSON-RPC 2.0) client built on
httpx — the `mcp` python SDK is not a dependency here. Streamable-HTTP responses
may arrive as application/json or as text/event-stream; both are handled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from core.types import (
    AssetClass,
    Order,
    OrderId,
    OrderSide,
    OrderType,
)
from execution.broker import (
    BrokerAccount,
    BrokerError,
    BrokerEventCallback,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerRejection,
    BrokerUnavailable,
)

log = logging.getLogger(__name__)

DEFAULT_MCP_URL = "https://agent.robinhood.com/mcp/trading"
_HTTP_TIMEOUT_SECS = 30.0

# Robinhood agentic account (agentic_allowed=True). Personal margin account
# 891728651 must NEVER be used for bot orders.
_AGENTIC_ACCOUNT = "981398050"

# Verified tool names (live probe 2026-06-10).
_RH_TOOLS = {
    "place_order":    "place_equity_order",
    "review_order":   "review_equity_order",
    "cancel_order":   "cancel_equity_order",
    "get_orders":     "get_equity_orders",
    "get_positions":  "get_equity_positions",
    "get_accounts":   "get_accounts",
    "get_portfolio":  "get_portfolio",
}

# Verified RH order state vocabulary (get_equity_orders response).
_RH_STATE_MAP: dict[str, BrokerOrderState] = {
    "new":              BrokerOrderState.NEW,
    "queued":           BrokerOrderState.NEW,
    "unconfirmed":      BrokerOrderState.NEW,
    "confirmed":        BrokerOrderState.ACCEPTED,
    "partially_filled": BrokerOrderState.PARTIALLY_FILLED,
    "filled":           BrokerOrderState.FILLED,
    "cancelled":        BrokerOrderState.CANCELED,
    "rejected":         BrokerOrderState.REJECTED,
    "failed":           BrokerOrderState.REJECTED,
    "voided":           BrokerOrderState.CANCELED,
    "canceled":         BrokerOrderState.CANCELED,   # American spelling alias
}


def _decimal(v: Any) -> Decimal:  # noqa: ANN401
    """Coerce a possibly-None / str / number value to Decimal, defaulting to 0."""
    if v is None or v == "":
        return Decimal("0")
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _data(resp: Any) -> dict[str, Any]:  # noqa: ANN401
    """Unwrap Robinhood's `{"data": {...}, "guide": "..."}` envelope.

    Every RH MCP tool wraps its payload under `data`. Returns the inner dict,
    or the response itself if it isn't enveloped (defensive).
    """
    if isinstance(resp, dict):
        inner = resp.get("data")
        if isinstance(inner, dict):
            return inner
        return resp
    return {}


def _data_list(resp: Any, key: str) -> list[Any]:  # noqa: ANN401
    """Return `data[key]` as a list (e.g. data.orders, data.positions)."""
    v = _data(resp).get(key)
    return v if isinstance(v, list) else []


# ── MCP client via the official SDK (OAuth + streamable HTTP) ─────────────────


class _FileTokenStorage(TokenStorage):
    """Persists OAuth tokens + dynamic client registration to a JSON file.

    Written by scripts/robinhood_mcp_connect.py during the one-time browser
    authorisation; read (and refreshed in place) by the bot at runtime.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        if path.exists():
            self._data = json.loads(path.read_text())

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2, default=str))
        with contextlib.suppress(Exception):
            self._path.chmod(0o600)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._data.get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json")
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._data.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._data["client_info"] = info.model_dump(mode="json")
        self._save()


def _extract_tool_dict(result: Any) -> dict[str, Any]:  # noqa: ANN401
    """Normalise an MCP CallToolResult into the dict the broker expects.

    Prefer structuredContent; else JSON-parse the first text block. Mirrors the
    prior hand-rolled client's contract so downstream parsing is unchanged.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {"text": text}
            except json.JSONDecodeError:
                return {"text": text}
    return {}


class _McpSdkClient:
    """Thread-safe synchronous facade over the async MCP SDK OAuth client.

    The OMS calls broker methods synchronously from worker threads, but the MCP
    SDK is async. We run a dedicated event loop in a background thread and bridge
    each call via run_coroutine_threadsafe. Tokens are loaded from disk and
    auto-refreshed by the SDK's OAuthClientProvider; no browser is opened at
    runtime — if a refresh ever fails, we raise a clear "re-auth" BrokerError.
    """

    def __init__(self, url: str, tokens_path: str) -> None:
        self._url = url
        self._storage = _FileTokenStorage(Path(tokens_path).expanduser())
        self._client_metadata = OAuthClientMetadata(
            client_name="Multi-Agent Asset Bot",
            redirect_uris=["http://localhost:4321/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope="internal",
        )
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="rh-mcp-loop"
        )
        self._thread.start()

    async def _reauth_required(self, *_: Any) -> Any:  # noqa: ANN401
        raise BrokerError(
            "Robinhood token expired and headless refresh failed. "
            "Re-run: uv run python scripts/robinhood_mcp_connect.py"
        )

    def _oauth(self) -> OAuthClientProvider:
        return OAuthClientProvider(
            server_url=self._url,
            client_metadata=self._client_metadata,
            storage=self._storage,
            redirect_handler=self._reauth_required,
            callback_handler=self._reauth_required,
        )

    async def _call_async(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with (
            streamablehttp_client(self._url, auth=self._oauth()) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
            if getattr(result, "isError", False):
                raise BrokerRejection(f"Robinhood MCP tool error: {_extract_tool_dict(result)}")
            return _extract_tool_dict(result)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke an MCP tool synchronously, returning the parsed structured result."""
        with self._lock:
            fut = asyncio.run_coroutine_threadsafe(
                self._call_async(name, arguments), self._loop
            )
            try:
                return fut.result(timeout=_HTTP_TIMEOUT_SECS * 2)
            except FutureTimeout as exc:
                raise BrokerUnavailable(f"Robinhood MCP call '{name}' timed out") from exc
            except (BrokerError, BrokerRejection, BrokerUnavailable):
                raise
            except Exception as exc:  # noqa: BLE001
                raise BrokerUnavailable(f"Robinhood MCP call '{name}' failed: {exc}") from exc

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._loop.call_soon_threadsafe(self._loop.stop)


# ── Broker adapter ────────────────────────────────────────────────────────────


class RobinhoodBroker:
    """Broker Protocol implementation backed by Robinhood's agentic MCP server.

    No push stream is available, so order/fill state propagates via the OMS's
    existing Reconciler polling (get_order / find_order_by_client_id). start_stream
    is therefore a no-op.
    """

    def __init__(
        self,
        mcp_client: _McpSdkClient | None = None,
        *,
        mcp_url: str = DEFAULT_MCP_URL,
        live_trading_enabled: bool = False,
    ) -> None:
        self._live = live_trading_enabled
        self._mcp = mcp_client
        self._callback: BrokerEventCallback | None = None
        # client_order_id (str) → broker_order_id. An entry is set to
        # _INFLIGHT before the MCP call and replaced with the real broker id
        # on success. Concurrent submits of the same order id see _INFLIGHT
        # and short-circuit; ref_id idempotency at the server handles any
        # race that slips through.
        self._submitted: dict[str, str] = {}
        # dry-run fill simulation: broker_id → Order, broker_id → fill_price
        self._dry_run_orders: dict[str, Order] = {}
        self._dry_run_fills: dict[str, Decimal | None] = {}
        self._lock = threading.Lock()
        mode = "LIVE" if self._live else "DRY-RUN"
        log.warning("RobinhoodBroker initialised in %s mode (url=%s).", mode, mcp_url)

    _INFLIGHT = "__INFLIGHT__"

    # ── idempotent submit ──────────────────────────────────────────────────────
    def submit_order(self, order: Order) -> str:
        client_id = str(order.id)
        with self._lock:
            existing = self._submitted.get(client_id)
            if existing is not None and existing != self._INFLIGHT:
                return existing
            if existing == self._INFLIGHT:
                # Another thread is mid-placement with the same order id.
                # ref_id idempotency at the server protects against the duplicate;
                # return a sentinel so the caller retries reconciliation.
                log.warning("submit_order: concurrent submit for %s; signalling retry", client_id)
                raise BrokerUnavailable(
                    f"Concurrent submit in progress for order {client_id}; retry"
                )
            self._submitted[client_id] = self._INFLIGHT  # claim before releasing lock

        if not self._live or self._mcp is None:
            broker_id = f"DRYRUN-{uuid.uuid4()}"
            log.info(
                "RobinhoodBroker DRY-RUN: would place %s %s qty=%s type=%s "
                "(ref_id=%s) → %s",
                order.side, order.symbol, order.qty, order.order_type,
                client_id, broker_id,
            )
            with self._lock:
                self._submitted[client_id] = broker_id
                self._dry_run_orders[broker_id] = order
            return broker_id

        args = self._build_order_args(order)

        # Simulate the order before placing (mandatory safety step).
        try:
            review = self._mcp.call_tool(_RH_TOOLS["review_order"], args)
            log.info("RobinhoodBroker review: %s", review)
        except Exception as exc:  # noqa: BLE001
            log.warning("RobinhoodBroker review_equity_order failed (proceeding): %s", exc)

        try:
            resp = self._mcp.call_tool(_RH_TOOLS["place_order"], args)
        except BrokerError:
            with self._lock:
                self._submitted.pop(client_id, None)  # release inflight claim
            raise
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._submitted.pop(client_id, None)
            raise BrokerUnavailable(f"Robinhood place_equity_order failed: {exc}") from exc

        # Robinhood returns the order id under "id"; "order_id" is a fallback
        # in case the field name changes in a future schema version.
        if not isinstance(resp, dict):
            with self._lock:
                self._submitted.pop(client_id, None)
            raise BrokerRejection(f"Robinhood place_equity_order non-dict response: {resp!r}")
        broker_id = str(resp.get("id") or resp.get("order_id") or "")
        if not broker_id:
            with self._lock:
                self._submitted.pop(client_id, None)
            raise BrokerRejection(
                f"Robinhood place_equity_order returned no id. Full response: {resp}"
            )
        with self._lock:
            self._submitted[client_id] = broker_id
        log.info(
            "RobinhoodBroker LIVE order placed: %s %s qty=%s → broker_id=%s",
            order.side, order.symbol, order.qty, broker_id,
        )
        return broker_id

    def _build_order_args(self, order: Order) -> dict[str, Any]:
        """Translate our Order → Robinhood place_equity_order arguments."""
        order_type = "market" if order.order_type == OrderType.MARKET else "limit"
        tif_raw = str(order.time_in_force).lower()
        time_in_force = "gtc" if "gtc" in tif_raw else "gfd"
        args: dict[str, Any] = {
            "account_number": _AGENTIC_ACCOUNT,
            "symbol": order.symbol,
            "side": "buy" if order.side == OrderSide.BUY else "sell",
            "type": order_type,
            "quantity": str(order.qty),
            "time_in_force": time_in_force,
            "ref_id": str(order.id),   # UUID idempotency key
        }
        if order.order_type != OrderType.MARKET and order.limit_price is not None:
            args["limit_price"] = str(order.limit_price)
        if order.stop_price is not None:
            args["stop_price"] = str(order.stop_price)
        return args

    # ── cancel ──────────────────────────────────────────────────────────────────
    def cancel_order(self, broker_order_id: str) -> None:
        if not self._live or self._mcp is None or broker_order_id.startswith("DRYRUN-"):
            log.info("RobinhoodBroker DRY-RUN: would cancel %s", broker_order_id)
            return
        try:
            self._mcp.call_tool(
                _RH_TOOLS["cancel_order"],
                {"account_number": _AGENTIC_ACCOUNT, "order_id": broker_order_id},
            )
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood cancel_equity_order failed: {exc}") from exc

    # ── status queries ──────────────────────────────────────────────────────────
    def get_order(self, broker_order_id: str) -> BrokerOrderStatus:
        if broker_order_id.startswith("DRYRUN-"):
            return self._simulate_fill(broker_order_id)
        if not self._live or self._mcp is None:
            raise BrokerUnavailable("RobinhoodBroker in dry-run; no live order to fetch")
        try:
            resp = self._mcp.call_tool(
                _RH_TOOLS["get_orders"],
                {"account_number": _AGENTIC_ACCOUNT, "order_id": broker_order_id},
            )
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood get_equity_orders failed: {exc}") from exc
        if not isinstance(resp, dict):
            raise BrokerUnavailable(f"Robinhood get_equity_orders non-dict response: {resp!r}")
        orders = _data_list(resp, "orders")
        if not orders:
            raise BrokerUnavailable(f"Robinhood returned no order for id={broker_order_id}")
        return self._translate_order(orders[0])

    def find_order_by_client_id(self, client_order_id: OrderId) -> BrokerOrderStatus | None:
        """Scan recent orders for a matching ref_id (our idempotency UUID).

        RH's get_equity_orders does not support filtering by ref_id, so we fetch
        the full account order list and match locally. Handles crash-recovery where
        broker_id was lost in-process.
        """
        if not self._live or self._mcp is None:
            return None
        try:
            resp = self._mcp.call_tool(
                _RH_TOOLS["get_orders"],
                {"account_number": _AGENTIC_ACCOUNT},
            )
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(resp, dict):
            return None
        for o in _data_list(resp, "orders"):
            if isinstance(o, dict) and str(o.get("ref_id", "")) == str(client_order_id):
                return self._translate_order(o)
        return None

    def list_positions(self) -> list[BrokerPosition]:
        if not self._live or self._mcp is None:
            raise BrokerUnavailable("RobinhoodBroker in dry-run; no live positions to fetch")
        try:
            resp = self._mcp.call_tool(
                _RH_TOOLS["get_positions"],
                {"account_number": _AGENTIC_ACCOUNT},
            )
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood get_equity_positions failed: {exc}") from exc
        raw = _data_list(resp, "positions")
        return [self._translate_position(p) for p in raw]

    def get_account(self) -> BrokerAccount:
        if not self._live or self._mcp is None:
            return BrokerAccount(
                cash=Decimal("0"), equity=Decimal("0"), buying_power=Decimal("0"),
                pattern_day_trader=False, daytrade_count=0,
            )
        # Balances come from get_portfolio, NOT get_accounts (which carries no
        # cash/equity/buying_power). The agentic account is a CASH account, so
        # PDT fields don't exist and default safely to False/0.
        try:
            resp = self._mcp.call_tool(
                _RH_TOOLS["get_portfolio"],
                {"account_number": _AGENTIC_ACCOUNT},
            )
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood get_portfolio failed: {exc}") from exc
        data = _data(resp)
        # buying_power is a nested object: data.buying_power.buying_power is the
        # authoritative spendable figure.
        bp_obj = data.get("buying_power")
        bp = bp_obj.get("buying_power") if isinstance(bp_obj, dict) else bp_obj
        return BrokerAccount(
            cash=_decimal(data.get("cash")),
            equity=_decimal(data.get("total_value")),
            buying_power=_decimal(bp),
            pattern_day_trader=False,   # cash account — no PDT concept
            daytrade_count=0,
        )

    def register_event_callback(self, callback: BrokerEventCallback) -> None:
        # Retained for Protocol compatibility; RH has no push stream, so the
        # Reconciler drives state. Stored in case a future webhook bridge fires it.
        self._callback = callback

    def start_stream(self) -> None:  # optional Broker extension; no-op for RH
        log.info("RobinhoodBroker: no push stream; relying on reconciler polling")

    def stop_stream(self) -> None:
        if self._mcp is not None:
            self._mcp.close()

    # ── dry-run fill simulation ─────────────────────────────────────────────────
    def _simulate_fill(self, broker_order_id: str) -> BrokerOrderStatus:
        """Return a synthetic FILLED status for a dry-run order.

        Price is fetched once from yfinance and then cached so repeated
        reconciler calls return a stable fill price. Falls back to the
        order's limit_price, then 0 if the market is closed or lookup fails.
        """
        with self._lock:
            order = self._dry_run_orders.get(broker_order_id)
            cached_price = self._dry_run_fills.get(broker_order_id)

        if order is None:
            raise BrokerUnavailable(f"Unknown dry-run order id: {broker_order_id}")

        # _dry_run_fills stores None as a sentinel meaning "lookup attempted but
        # unavailable". Only fetch once; use broker_order_id not in dict to distinguish
        # first call from a cached None.
        if broker_order_id not in self._dry_run_fills:
            price = self._fetch_last_price(order.symbol, order.limit_price)
            with self._lock:
                self._dry_run_fills[broker_order_id] = price
            cached_price = price
        # cached_price may legitimately be None (price unavailable)

        now = datetime.now(UTC)
        return BrokerOrderStatus(
            broker_order_id=broker_order_id,
            client_order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            filled_qty=order.qty,
            avg_fill_price=cached_price if cached_price is not None else None,
            state=BrokerOrderState.FILLED,
            submitted_at=now,
            updated_at=now,
            rejection_reason=None,
        )

    @staticmethod
    def _fetch_last_price(symbol: str, fallback: Decimal | None) -> Decimal | None:
        """Return last traded price from yfinance, fallback limit price, or None.

        Returns None (not zero) when price is unavailable so callers can
        distinguish "no price" from "zero price" — Decimal(0) is falsy.
        """
        try:
            import yfinance as yf  # noqa: PLC0415
            ticker = yf.Ticker(symbol)
            price = ticker.fast_info.get("last_price") or ticker.fast_info.get("regularMarketPrice")
            if price:
                return Decimal(str(price))
        except Exception:  # noqa: BLE001
            pass
        return fallback  # may be None

    # ── translation helpers ─────────────────────────────────────────────────────
    def _translate_order(self, o: dict[str, Any]) -> BrokerOrderStatus:
        state = _RH_STATE_MAP.get(str(o.get("state", "")).lower(), BrokerOrderState.UNKNOWN)
        side = OrderSide.BUY if str(o.get("side", "buy")).lower() == "buy" else OrderSide.SELL
        now = datetime.now(UTC)
        return BrokerOrderStatus(
            broker_order_id=str(o.get("id", "")),
            client_order_id=_to_uuid(o.get("ref_id")),    # ref_id is our UUID
            symbol=str(o.get("symbol", "")),
            side=side,
            qty=_decimal(o.get("quantity")),
            # RH reports filled qty as `cumulative_quantity` (per get_equity_orders
            # guide). The reconciler keys fills off this — reading the wrong field
            # would make every fill invisible. Fallback kept for safety.
            filled_qty=_decimal(o.get("cumulative_quantity") or o.get("filled_quantity")),
            avg_fill_price=_decimal(o.get("average_price")) or None,
            state=state,
            submitted_at=_parse_ts(o.get("created_at")) or now,
            updated_at=_parse_ts(o.get("last_transaction_at") or o.get("updated_at")) or now,
            rejection_reason=o.get("reject_reason"),
        )

    def _translate_position(self, p: dict[str, Any]) -> BrokerPosition:
        # NOTE: get_equity_positions carries NO market price — current_price is
        # best-effort here and will be 0 unless RH later adds it. For live
        # valuation/PnL, current price must come from get_equity_quotes (TODO).
        # qty uses `quantity` (total held) for reconciliation, not
        # shares_available_for_sells.
        return BrokerPosition(
            symbol=str(p.get("symbol", "")),
            qty=_decimal(p.get("quantity")),
            avg_entry_price=_decimal(p.get("average_buy_price")),
            current_price=_decimal(p.get("market_value") or p.get("current_price")),
            asset_class=AssetClass.CRYPTO if _looks_crypto(str(p.get("symbol", "")))
            else AssetClass.EQUITY,
        )


def _to_uuid(v: Any) -> uuid.UUID:  # noqa: ANN401
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        return uuid.uuid4()  # random; prevents crash-recovery collisions on malformed ref_id


def _parse_ts(v: Any) -> datetime | None:  # noqa: ANN401
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _looks_crypto(symbol: str) -> bool:
    return symbol.upper().endswith(("USD", "USDT")) and len(symbol) >= 6
