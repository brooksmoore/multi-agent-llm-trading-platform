"""Tests for ops/telegram.py — TelegramAdapter.

Uses an injected httpx.Client mock so no real network calls are made.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import httpx

from core.events import (
    EventBus,
    FillReceivedEvent,
    KillSwitchTrippedEvent,
    ReconciliationBreakEvent,
)
from core.types import AgentId, Fill, KillSwitchState, OrderSide, new_id
from ops.alerts import AlertManager
from ops.telegram import TelegramAdapter, _escape

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok_response() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {}})


def _err_response(status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "description": "Bad Request"})


def _make_adapter(
    *,
    enabled: bool = True,
    dedupe_window_secs: float = 60.0,
) -> tuple[TelegramAdapter, MagicMock]:
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value = _ok_response()
    token = "TOKEN" if enabled else ""
    chat_id = "12345" if enabled else ""
    adapter = TelegramAdapter(
        token, chat_id,
        http_client=mock_client,
        dedupe_window_secs=dedupe_window_secs,
    )
    return adapter, mock_client


# ── Escape helper ─────────────────────────────────────────────────────────────


class TestEscape:
    def test_plain_text_unchanged(self) -> None:
        assert _escape("hello world") == "hello world"

    def test_escapes_period(self) -> None:
        assert _escape("3.14") == r"3\.14"

    def test_escapes_exclamation(self) -> None:
        assert _escape("Watch out!") == r"Watch out\!"

    def test_escapes_parentheses(self) -> None:
        assert _escape("(test)") == r"\(test\)"

    def test_escapes_multiple_specials(self) -> None:
        result = _escape("SPY +1.2%")
        assert "\\+" in result
        assert r"\." in result

    def test_backslash_itself_escaped(self) -> None:
        assert _escape("a\\b") == r"a\\b"


# ── Disabled adapter (no credentials) ────────────────────────────────────────


class TestDisabledAdapter:
    def test_enabled_property_false_when_no_credentials(self) -> None:
        adapter = TelegramAdapter("", "")
        assert adapter.enabled is False

    def test_send_returns_false_when_disabled(self) -> None:
        adapter = TelegramAdapter("", "")
        assert adapter.send("hello") is False

    def test_halt_alert_returns_false_when_disabled(self) -> None:
        adapter = TelegramAdapter("", "")
        assert adapter.send_halt_alert("HALT reason") is False

    def test_fill_notification_returns_false_when_disabled(self) -> None:
        adapter = TelegramAdapter("", "")
        assert adapter.send_fill_notification("HAIKU", "SPY", "buy", "10", "500.00") is False

    def test_no_http_call_when_disabled(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        adapter = TelegramAdapter("", "", http_client=mock_client)
        adapter.send("test")
        mock_client.post.assert_not_called()


# ── Enabled adapter ───────────────────────────────────────────────────────────


class TestEnabledAdapter:
    def test_enabled_property_true_with_credentials(self) -> None:
        adapter, _ = _make_adapter()
        assert adapter.enabled is True

    def test_send_posts_to_telegram_api(self) -> None:
        adapter, mock_client = _make_adapter()
        result = adapter.send("Hello MarkdownV2")
        assert result is True
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "sendMessage" in call_kwargs[0][0]
        payload = call_kwargs[1]["json"]
        assert payload["parse_mode"] == "MarkdownV2"
        assert payload["chat_id"] == "12345"

    def test_send_returns_false_on_api_error(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.post.return_value = _err_response(400)
        result = adapter.send("failing message")
        assert result is False

    def test_send_returns_false_on_network_exception(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.post.side_effect = httpx.ConnectError("timeout")
        result = adapter.send("failing message")
        assert result is False

    def test_message_truncated_at_4096_chars(self) -> None:
        adapter, mock_client = _make_adapter()
        long_text = "A" * 5000
        adapter.send(long_text)
        payload = mock_client.post.call_args[1]["json"]
        assert len(payload["text"]) <= 4096

    def test_halt_alert_includes_inline_keyboard(self) -> None:
        adapter, mock_client = _make_adapter()
        adapter.send_halt_alert("RiskGate tripped", agent="SONNET")
        payload = mock_client.post.call_args[1]["json"]
        assert "reply_markup" in payload
        keyboard = payload["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 1
        button = keyboard[0][0]
        assert "Acknowledge" in button["text"]
        assert button["url"].startswith("https://")

    def test_halt_alert_escapes_reason(self) -> None:
        adapter, mock_client = _make_adapter()
        adapter.send_halt_alert("drawdown > 10%!")
        payload = mock_client.post.call_args[1]["json"]
        text = payload["text"]
        # ">" and "!" must be escaped in MarkdownV2
        assert "\\>" in text or ">" not in text
        assert "\\!" in text

    def test_fill_notification_formats_correctly(self) -> None:
        adapter, mock_client = _make_adapter()
        result = adapter.send_fill_notification("HAIKU", "TQQQ", "buy", "5.5", "42.00")
        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        text = payload["text"]
        assert "FILL" in text
        assert "TQQQ" in text
        assert "BUY" in text

    def test_close_closes_http_client(self) -> None:
        adapter, mock_client = _make_adapter()
        adapter.close()
        mock_client.close.assert_called_once()


# ── Deduplication ─────────────────────────────────────────────────────────────


class TestDeduplication:
    def test_same_key_suppressed_within_window(self) -> None:
        adapter, mock_client = _make_adapter(dedupe_window_secs=60.0)
        adapter.send("msg", key="k1")
        adapter.send("msg", key="k1")
        assert mock_client.post.call_count == 1

    def test_different_keys_both_sent(self) -> None:
        adapter, mock_client = _make_adapter(dedupe_window_secs=60.0)
        adapter.send("msg A", key="k1")
        adapter.send("msg B", key="k2")
        assert mock_client.post.call_count == 2

    def test_same_key_allowed_after_window_expires(self) -> None:
        adapter, mock_client = _make_adapter(dedupe_window_secs=0.05)
        adapter.send("msg", key="expire")
        time.sleep(0.1)
        adapter.send("msg", key="expire")
        assert mock_client.post.call_count == 2

    def test_dedup_uses_text_hash_when_no_key(self) -> None:
        adapter, mock_client = _make_adapter(dedupe_window_secs=60.0)
        adapter.send("identical text")
        adapter.send("identical text")
        assert mock_client.post.call_count == 1

    def test_different_texts_no_key_both_sent(self) -> None:
        adapter, mock_client = _make_adapter(dedupe_window_secs=60.0)
        adapter.send("text one")
        adapter.send("text two")
        assert mock_client.post.call_count == 2


# ── AlertManager integration ──────────────────────────────────────────────────


class TestAlertManagerTelegramIntegration:
    def _make_bus_and_manager(
        self, enabled: bool = True
    ) -> tuple[EventBus, AlertManager, MagicMock]:
        bus = EventBus()
        adapter, mock_client = _make_adapter(enabled=enabled)
        manager = AlertManager(bus, topic="", telegram=adapter)
        manager.start()
        return bus, manager, mock_client

    def test_kill_switch_does_not_immediately_send_halt_alert(self) -> None:
        # Policy: Telegram is reserved for stuck halts (>30 min). Routine
        # transient trips roll into the daily recap, not real-time push.
        bus, manager, mock_client = self._make_bus_and_manager()
        bus.publish(KillSwitchTrippedEvent(
            agent_id=AgentId.HAIKU,
            new_state=KillSwitchState.DRAWDOWN_PAUSED,
            reason="drawdown threshold breached",
        ))
        mock_client.post.assert_not_called()
        manager.stop()

    def test_fill_received_does_not_send_notification(self) -> None:
        # Per-fill Telegram notifications were disabled in favor of an
        # hourly portfolio snapshot; AlertManager no longer subscribes to
        # fill.received. The send_fill_notification helper still exists
        # for callers who want to opt back in.
        bus, manager, mock_client = self._make_bus_and_manager()
        fill = Fill(
            id=new_id(),
            order_id=new_id(),
            agent_id=AgentId.SONNET,
            symbol="SPY",
            side=OrderSide.BUY,
            qty=Decimal("10"),
            price=Decimal("500.00"),
            timestamp=datetime.now(UTC),
        )
        bus.publish(FillReceivedEvent(fill=fill))
        mock_client.post.assert_not_called()
        manager.stop()

    def test_fill_not_subscribed_when_telegram_disabled(self) -> None:
        bus, manager, mock_client = self._make_bus_and_manager(enabled=False)
        fill = Fill(
            id=new_id(),
            order_id=new_id(),
            agent_id=AgentId.HAIKU,
            symbol="SPY",
            side=OrderSide.BUY,
            qty=Decimal("5"),
            price=Decimal("400.00"),
            timestamp=datetime.now(UTC),
        )
        bus.publish(FillReceivedEvent(fill=fill))
        mock_client.post.assert_not_called()
        manager.stop()

    def test_reconciliation_break_does_not_send_telegram(self) -> None:
        # Policy: recon breaks are auto-handled; they ntfy only and roll
        # into the daily recap. No Telegram push regardless of delta.
        bus, manager, mock_client = self._make_bus_and_manager()
        bus.publish(ReconciliationBreakEvent(
            symbol="AAPL",
            local_qty=Decimal("10"),
            broker_qty=Decimal("11"),
            delta_usd=Decimal("200"),
        ))
        mock_client.post.assert_not_called()
        manager.stop()

    def test_stuck_halt_telegrams_after_grace_window(self) -> None:
        # With a tiny grace window the deferred timer should fire and Telegram
        # should be invoked exactly once.
        bus = EventBus()
        adapter, mock_client = _make_adapter(enabled=True)
        manager = AlertManager(
            bus, topic="", telegram=adapter, halt_telegram_grace_secs=0.05,
        )
        manager.start()
        try:
            bus.publish(KillSwitchTrippedEvent(
                agent_id=AgentId.HAIKU,
                new_state=KillSwitchState.DRAWDOWN_PAUSED,
                reason="stuck condition",
            ))
            import time as _t  # noqa: PLC0415
            _t.sleep(0.20)
            mock_client.post.assert_called_once()
        finally:
            manager.stop()

    def test_stuck_halt_cancelled_when_kill_switch_resets(self) -> None:
        bus = EventBus()
        adapter, mock_client = _make_adapter(enabled=True)
        manager = AlertManager(
            bus, topic="", telegram=adapter, halt_telegram_grace_secs=0.20,
        )
        manager.start()
        try:
            from core.events import KillSwitchResetEvent  # noqa: PLC0415
            bus.publish(KillSwitchTrippedEvent(
                agent_id=AgentId.HAIKU,
                new_state=KillSwitchState.DRAWDOWN_PAUSED,
                reason="transient",
            ))
            bus.publish(KillSwitchResetEvent(agent_id=AgentId.HAIKU))
            import time as _t  # noqa: PLC0415
            _t.sleep(0.30)
            mock_client.post.assert_not_called()
            assert manager._halt_suppressed_count == 1
        finally:
            manager.stop()
