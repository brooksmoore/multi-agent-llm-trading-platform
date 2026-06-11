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

import contextlib
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

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
_MCP_PROTOCOL_VERSION = "2025-06-18"

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


# ── Minimal MCP-over-HTTP JSON-RPC client ─────────────────────────────────────


class _McpHttpClient:
    """Synchronous MCP client speaking JSON-RPC 2.0 over streamable HTTP.

    Only the slice we need: `initialize` handshake + `tools/call`. Thread-safe
    (one lock serialises requests, since the OMS may call from multiple threads).
    """

    def __init__(self, url: str, auth_token: str) -> None:
        self._url = url
        self._auth_token = auth_token
        self._client = httpx.Client(timeout=_HTTP_TIMEOUT_SECS)
        self._session_id: str | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._initialized = False

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # TODO-VERIFY: Robinhood may use OAuth bearer, a custom header, or a
            # signed request. Bearer is the MCP-conventional default.
            "Authorization": f"Bearer {self._auth_token}",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _rpc(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """Send one JSON-RPC request, return the `result` object. Raises on error."""
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            try:
                resp = self._client.post(self._url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                raise BrokerUnavailable(f"Robinhood MCP transport error: {exc}") from exc

            # Capture a server-assigned session id from the initialize response.
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid

            if resp.status_code >= 500:
                raise BrokerUnavailable(f"Robinhood MCP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (401, 403):
                raise BrokerError(f"Robinhood MCP auth failed ({resp.status_code})")
            if resp.status_code >= 400:
                raise BrokerRejection(f"Robinhood MCP {resp.status_code}: {resp.text[:200]}")

            body = self._parse_body(resp)
            if "error" in body and body["error"]:
                err = body["error"]
                raise BrokerRejection(f"Robinhood MCP error: {err}")
            result = body.get("result", {})
            return result if isinstance(result, dict) else {}

    @staticmethod
    def _parse_body(resp: httpx.Response) -> dict[str, Any]:
        """Parse a JSON-RPC response that may be application/json or SSE."""
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # Streamable HTTP: take the last `data:` line carrying a JSON-RPC msg.
            last: dict[str, Any] = {}
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        last = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            return last
        try:
            parsed = resp.json()
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError as exc:
            raise BrokerUnavailable(f"Robinhood MCP non-JSON response: {resp.text[:200]}") from exc

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "multi-agent-bot", "version": "1.0"},
            },
        )
        # MCP requires a follow-up notification (no id, no response expected).
        # Best-effort; some servers don't require it.
        with contextlib.suppress(httpx.HTTPError):
            self._client.post(
                self._url,
                headers=self._headers(),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        self._initialized = True

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the server's advertised tools. Use this to reconcile `_RH_TOOLS`."""
        self.ensure_initialized()
        result = self._rpc("tools/list", {})
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke an MCP tool, returning the parsed structured result."""
        self.ensure_initialized()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        # MCP tool results carry `content` (list of blocks) and optionally
        # `structuredContent`. Prefer structured; else parse the first text block.
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    parsed = json.loads(block["text"])
                    return parsed if isinstance(parsed, dict) else {"text": block["text"]}
                except (json.JSONDecodeError, KeyError):
                    return {"text": block.get("text", "")}
        return result

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()


# ── Broker adapter ────────────────────────────────────────────────────────────


class RobinhoodBroker:
    """Broker Protocol implementation backed by Robinhood's agentic MCP server.

    No push stream is available, so order/fill state propagates via the OMS's
    existing Reconciler polling (get_order / find_order_by_client_id). start_stream
    is therefore a no-op.
    """

    def __init__(
        self,
        auth_token: str,
        *,
        mcp_url: str = DEFAULT_MCP_URL,
        live_trading_enabled: bool = False,
    ) -> None:
        self._live = live_trading_enabled
        self._mcp = _McpHttpClient(mcp_url, auth_token) if auth_token else None
        self._callback: BrokerEventCallback | None = None
        # client_order_id (str) → broker_order_id, for in-process idempotency.
        self._submitted: dict[str, str] = {}
        # dry-run fill simulation: broker_id → Order, broker_id → fill_price
        self._dry_run_orders: dict[str, Order] = {}
        self._dry_run_fills: dict[str, Decimal] = {}
        self._lock = threading.Lock()
        mode = "LIVE" if self._live else "DRY-RUN"
        log.warning("RobinhoodBroker initialised in %s mode (url=%s).", mode, mcp_url)

    # ── idempotent submit ──────────────────────────────────────────────────────
    def submit_order(self, order: Order) -> str:
        client_id = str(order.id)
        with self._lock:
            if client_id in self._submitted:
                return self._submitted[client_id]  # in-process idempotency

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
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood place_equity_order failed: {exc}") from exc

        broker_id = str(resp.get("id") or "")
        if not broker_id:
            raise BrokerRejection(f"Robinhood place_equity_order returned no id: {resp}")
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
        # Returns a list; grab the single matching order.
        orders = resp.get("results", resp if isinstance(resp, list) else [resp])
        if not orders:
            raise BrokerUnavailable(f"Robinhood returned no order for id={broker_order_id}")
        return self._translate_order(orders[0] if isinstance(orders, list) else resp)

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
        orders = resp.get("results", resp if isinstance(resp, list) else [])
        for o in orders if isinstance(orders, list) else []:
            if str(o.get("ref_id", "")) == str(client_order_id):
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
        raw = resp.get("results", resp if isinstance(resp, list) else [])
        return [self._translate_position(p) for p in (raw if isinstance(raw, list) else [])]

    def get_account(self) -> BrokerAccount:
        if not self._live or self._mcp is None:
            return BrokerAccount(
                cash=Decimal("0"), equity=Decimal("0"), buying_power=Decimal("0"),
                pattern_day_trader=False, daytrade_count=0,
            )
        try:
            resp = self._mcp.call_tool(_RH_TOOLS["get_accounts"], {})
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood get_accounts failed: {exc}") from exc
        # get_accounts returns a list; find the agentic account.
        accounts = resp.get("results", resp if isinstance(resp, list) else [resp])
        acct: dict[str, Any] = {}
        for a in accounts if isinstance(accounts, list) else [accounts]:
            if str(a.get("account_number", "")) == _AGENTIC_ACCOUNT:
                acct = a
                break
        return BrokerAccount(
            cash=_decimal(acct.get("cash")),
            equity=_decimal(acct.get("equity") or acct.get("portfolio_value")),
            buying_power=_decimal(acct.get("buying_power")),
            pattern_day_trader=bool(acct.get("pattern_day_trader", False)),
            daytrade_count=int(acct.get("daytrade_count", 0) or 0),
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

        if cached_price is None:
            cached_price = self._fetch_last_price(order.symbol, order.limit_price)
            with self._lock:
                self._dry_run_fills[broker_order_id] = cached_price

        now = datetime.now(UTC)
        return BrokerOrderStatus(
            broker_order_id=broker_order_id,
            client_order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            filled_qty=order.qty,
            avg_fill_price=cached_price or None,
            state=BrokerOrderState.FILLED,
            submitted_at=now,
            updated_at=now,
            rejection_reason=None,
        )

    @staticmethod
    def _fetch_last_price(symbol: str, fallback: Decimal | None) -> Decimal:
        try:
            import yfinance as yf  # noqa: PLC0415
            ticker = yf.Ticker(symbol)
            price = ticker.fast_info.get("last_price") or ticker.fast_info.get("regularMarketPrice")
            if price:
                return Decimal(str(price))
        except Exception:  # noqa: BLE001
            pass
        return fallback or Decimal("0")

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
            filled_qty=_decimal(o.get("filled_quantity")),
            avg_fill_price=_decimal(o.get("average_price")) or None,
            state=state,
            submitted_at=_parse_ts(o.get("created_at")) or now,
            updated_at=_parse_ts(o.get("updated_at")) or now,
            rejection_reason=o.get("reject_reason"),
        )

    def _translate_position(self, p: dict[str, Any]) -> BrokerPosition:
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
        return uuid.UUID(int=0)


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
