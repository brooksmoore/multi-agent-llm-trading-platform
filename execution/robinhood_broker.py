"""RobinhoodBroker: Broker adapter for Robinhood's agentic trading MCP endpoint.

Wraps Robinhood's MCP server (`agent.robinhood.com/mcp/trading`) behind the same
`Broker` Protocol the OMS already uses for AlpacaBroker / FakeBroker. The OMS and
the rest of the system are unaware of the transport.

────────────────────────────────────────────────────────────────────────────────
⚠️  TWO THINGS YOU MUST KNOW BEFORE FUNDING THIS WITH REAL MONEY
────────────────────────────────────────────────────────────────────────────────
1.  SAFETY GATE — `live_trading_enabled` defaults to False. In that state every
    submit_order() is a DRY RUN: it logs the order it *would* have placed, returns
    a synthetic `DRYRUN-<uuid>` broker id, and sends NOTHING to Robinhood. You must
    explicitly pass live_trading_enabled=True (and a real auth token) to place live
    orders. Arming this is the operator's decision, never made implicitly.

2.  UNVERIFIED SCHEMA — Robinhood's agentic MCP is brand new and its exact tool
    names, argument shapes, auth flow, and response fields are NOT yet confirmed
    against documentation. Every such assumption below is tagged `# TODO-VERIFY`.
    Treat the order-placement path as UNPROVEN until you have:
      (a) called list_tools() against the live server and reconciled `_RH_TOOLS`,
      (b) placed a single 1-share dry-run→live test and inspected the response,
      (c) confirmed idempotency behaviour (does RH honour a client order id?).

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
_MCP_PROTOCOL_VERSION = "2025-06-18"  # TODO-VERIFY against server's supported version


# ── Robinhood MCP tool names — TODO-VERIFY every one of these ──────────────────
# These are best-guess names. Reconcile against list_tools() output from the live
# server before enabling live trading. Keeping them in one dict makes that a
# one-place fix.
_RH_TOOLS = {
    "place_order": "place_order",            # TODO-VERIFY
    "cancel_order": "cancel_order",          # TODO-VERIFY
    "get_order": "get_order",                # TODO-VERIFY
    "list_orders": "list_orders",            # TODO-VERIFY
    "list_positions": "list_positions",      # TODO-VERIFY
    "get_account": "get_account",            # TODO-VERIFY
}

# Map Robinhood-reported order states → our canonical BrokerOrderState.
# TODO-VERIFY: confirm RH's actual status vocabulary.
_RH_STATE_MAP: dict[str, BrokerOrderState] = {
    "new": BrokerOrderState.NEW,
    "queued": BrokerOrderState.NEW,
    "pending": BrokerOrderState.NEW,
    "accepted": BrokerOrderState.ACCEPTED,
    "confirmed": BrokerOrderState.ACCEPTED,
    "partially_filled": BrokerOrderState.PARTIALLY_FILLED,
    "partial": BrokerOrderState.PARTIALLY_FILLED,
    "filled": BrokerOrderState.FILLED,
    "executed": BrokerOrderState.FILLED,
    "canceled": BrokerOrderState.CANCELED,
    "cancelled": BrokerOrderState.CANCELED,
    "rejected": BrokerOrderState.REJECTED,
    "failed": BrokerOrderState.REJECTED,
    "expired": BrokerOrderState.EXPIRED,
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
        self._lock = threading.Lock()
        mode = "LIVE" if self._live else "DRY-RUN"
        log.warning(
            "RobinhoodBroker initialised in %s mode (url=%s). "
            "Order schema is UNVERIFIED until reconciled against list_tools().",
            mode, mcp_url,
        )

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
                "(client_id=%s) → %s",
                order.side, order.symbol, order.qty, order.order_type,
                client_id, broker_id,
            )
            with self._lock:
                self._submitted[client_id] = broker_id
            return broker_id

        args = self._build_order_args(order)
        try:
            resp = self._mcp.call_tool(_RH_TOOLS["place_order"], args)
        except BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood place_order failed: {exc}") from exc

        broker_id = str(resp.get("id") or resp.get("order_id") or "")  # TODO-VERIFY
        if not broker_id:
            raise BrokerRejection(f"Robinhood place_order returned no id: {resp}")
        with self._lock:
            self._submitted[client_id] = broker_id
        log.info(
            "RobinhoodBroker LIVE order placed: %s %s qty=%s → broker_id=%s",
            order.side, order.symbol, order.qty, broker_id,
        )
        return broker_id

    def _build_order_args(self, order: Order) -> dict[str, Any]:
        """Translate our Order → Robinhood place_order arguments.

        TODO-VERIFY: argument names and accepted values are assumptions. Confirm
        against the live tool's inputSchema (list_tools()).
        """
        args: dict[str, Any] = {
            "symbol": order.symbol,
            "side": "buy" if order.side == OrderSide.BUY else "sell",
            "quantity": str(order.qty),
            "type": "market" if order.order_type == OrderType.MARKET else "limit",
            "time_in_force": str(order.time_in_force),
            # Pass our UUID as the client/idempotency key IF supported.
            "client_order_id": str(order.id),  # TODO-VERIFY name
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
            # TODO-VERIFY: tool name + arg key for cancellation.
            self._mcp.call_tool(_RH_TOOLS["cancel_order"], {"order_id": broker_order_id})
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood cancel_order failed: {exc}") from exc

    # ── status queries ──────────────────────────────────────────────────────────
    def get_order(self, broker_order_id: str) -> BrokerOrderStatus:
        if not self._live or self._mcp is None or broker_order_id.startswith("DRYRUN-"):
            raise BrokerUnavailable("RobinhoodBroker in dry-run; no live order to fetch")
        try:
            # TODO-VERIFY: tool name + arg key for single-order lookup.
            resp = self._mcp.call_tool(_RH_TOOLS["get_order"], {"order_id": broker_order_id})
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood get_order failed: {exc}") from exc
        return self._translate_order(resp)

    def find_order_by_client_id(self, client_order_id: OrderId) -> BrokerOrderStatus | None:
        if not self._live or self._mcp is None:
            return None
        try:
            resp = self._mcp.call_tool(
                _RH_TOOLS["list_orders"], {"client_order_id": str(client_order_id)},  # TODO-VERIFY
            )
        except Exception:  # noqa: BLE001
            return None
        orders = resp.get("orders", resp if isinstance(resp, list) else [])
        for o in orders if isinstance(orders, list) else []:
            if str(o.get("client_order_id")) == str(client_order_id):
                return self._translate_order(o)
        return None

    def list_positions(self) -> list[BrokerPosition]:
        if not self._live or self._mcp is None:
            return []
        try:
            resp = self._mcp.call_tool(_RH_TOOLS["list_positions"], {})
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood list_positions failed: {exc}") from exc
        raw = resp.get("positions", resp if isinstance(resp, list) else [])
        return [self._translate_position(p) for p in (raw if isinstance(raw, list) else [])]

    def get_account(self) -> BrokerAccount:
        if not self._live or self._mcp is None:
            # Dry-run: report a neutral account so callers don't crash.
            return BrokerAccount(
                cash=Decimal("0"), equity=Decimal("0"), buying_power=Decimal("0"),
                pattern_day_trader=False, daytrade_count=0,
            )
        try:
            resp = self._mcp.call_tool(_RH_TOOLS["get_account"], {})
        except Exception as exc:  # noqa: BLE001
            raise BrokerUnavailable(f"Robinhood get_account failed: {exc}") from exc
        return BrokerAccount(
            cash=_decimal(resp.get("cash") or resp.get("buying_power")),       # TODO-VERIFY
            equity=_decimal(resp.get("equity") or resp.get("portfolio_value")),  # TODO-VERIFY
            buying_power=_decimal(resp.get("buying_power")),                    # TODO-VERIFY
            pattern_day_trader=bool(resp.get("pattern_day_trader", False)),     # TODO-VERIFY
            daytrade_count=int(resp.get("daytrade_count", 0) or 0),             # TODO-VERIFY
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

    # ── translation helpers ─────────────────────────────────────────────────────
    def _translate_order(self, o: dict[str, Any]) -> BrokerOrderStatus:
        state = _RH_STATE_MAP.get(str(o.get("state") or o.get("status", "")).lower(),
                                  BrokerOrderState.UNKNOWN)  # TODO-VERIFY field name
        side = OrderSide.BUY if str(o.get("side", "buy")).lower() == "buy" else OrderSide.SELL
        now = datetime.now(UTC)
        return BrokerOrderStatus(
            broker_order_id=str(o.get("id") or o.get("order_id", "")),
            client_order_id=_to_uuid(o.get("client_order_id")),
            symbol=str(o.get("symbol", "")),
            side=side,
            qty=_decimal(o.get("quantity") or o.get("qty")),
            filled_qty=_decimal(o.get("filled_quantity") or o.get("filled_qty")),
            avg_fill_price=(_decimal(o.get("average_price") or o.get("avg_fill_price"))
                            or None),
            state=state,
            submitted_at=_parse_ts(o.get("created_at")) or now,
            updated_at=_parse_ts(o.get("updated_at")) or now,
            rejection_reason=o.get("reject_reason") or o.get("rejection_reason"),
        )

    def _translate_position(self, p: dict[str, Any]) -> BrokerPosition:
        return BrokerPosition(
            symbol=str(p.get("symbol", "")),
            qty=_decimal(p.get("quantity") or p.get("qty")),
            avg_entry_price=_decimal(p.get("average_buy_price") or p.get("avg_entry_price")),
            current_price=_decimal(p.get("market_price") or p.get("current_price")),
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
