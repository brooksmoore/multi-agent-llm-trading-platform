"""Outbound Telegram notification adapter.

Sends messages to a Telegram chat using the Bot API directly via httpx.
No polling or webhook required — purely outbound.

Configure via .env:
    TELEGRAM_BOT_TOKEN=<token from @BotFather>
    TELEGRAM_CHAT_ID=<numeric chat / group ID>

MarkdownV2 formatting is used throughout; all dynamic strings are escaped
via _escape() before insertion so malformed text never causes an API error.

Deduplication: the same dedup key is suppressed for dedupe_window_secs (60 s
default) so a flapping kill-switch does not flood the chat.

HALT alerts include a Telegram URL inline button ("Acknowledge") that opens
the Telegram app. This is the correct outbound-only approach: a url-type
button does not require a webhook to be functional.

Graceful degradation: every network error is caught and logged. The adapter
never raises — callers can ignore the return value.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import threading
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"
_HTTP_TIMEOUT = 5.0        # seconds per call
_DEDUPE_WINDOW = 60.0      # seconds
_MAX_CHARS = 4096          # Telegram hard message limit

# All characters that must be escaped in MarkdownV2 mode per Telegram docs.
_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#\+\-=|{}.!\\])")


def _escape(text: str) -> str:
    """Escape all MarkdownV2 reserved characters in a plain-text string."""
    return _MDV2_SPECIAL.sub(r"\\\1", text)


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_CHARS else text[: _MAX_CHARS - 1] + "…"


class TelegramAdapter:
    """Outbound-only Telegram notification adapter.

    Thread-safe. Never raises. All sends return True (delivered) or False
    (disabled / deduped / network error).

    Pass an httpx.Client via http_client to inject a mock in tests.
    """

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        *,
        http_client: httpx.Client | None = None,
        dedupe_window_secs: float = _DEDUPE_WINDOW,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        self._http: httpx.Client | None = (
            http_client if http_client is not None
            else (httpx.Client(timeout=_HTTP_TIMEOUT) if self._enabled else None)
        )
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        self._dedupe_window = dedupe_window_secs

        if not self._enabled:
            log.debug("TelegramAdapter: no credentials — notifications disabled")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Public API ─────────────────────────────────────────────────────────────

    def send(
        self,
        text: str,
        *,
        key: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """Send a pre-formatted MarkdownV2 message.

        key: dedup key — re-sends within dedupe_window_secs are suppressed.
             Defaults to an MD5 of the first 200 chars of text.
        reply_markup: optional Telegram reply_markup dict (e.g. inline keyboard).
        """
        if not self._enabled:
            log.debug("TelegramAdapter.send [disabled]: %.80s", text)
            return False

        dedup_key = key or hashlib.md5(text[:200].encode(), usedforsecurity=False).hexdigest()
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(dedup_key)
            if last is not None and (now - last) < self._dedupe_window:
                log.debug("TelegramAdapter: deduped key='%s'", dedup_key)
                return False
            self._last_sent[dedup_key] = now

        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": _truncate(text),
            "parse_mode": "MarkdownV2",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        url = f"{_TELEGRAM_API}/bot{self._token}/sendMessage"
        try:
            assert self._http is not None  # guarded by self._enabled check above
            resp = self._http.post(url, json=payload)
            if not resp.is_success:
                log.warning(
                    "TelegramAdapter: API error %d — %.200s",
                    resp.status_code,
                    resp.text,
                )
                return False
        except Exception:
            log.warning("TelegramAdapter: send failed", exc_info=True)
            return False

        log.debug("TelegramAdapter: sent key='%s'", dedup_key)
        return True

    def send_halt_alert(self, reason: str, agent: str | None = None) -> bool:
        """Send a HALT alert with an inline Acknowledge URL button."""
        agent_part = f" \\[{_escape(agent)}\\]" if agent else ""
        text = f"🚨 *HALT*{agent_part}\n{_escape(reason)}"
        markup = {
            "inline_keyboard": [[
                {"text": "✅ Acknowledge", "url": "https://t.me"},
            ]]
        }
        key = f"halt:{agent}:{reason[:60]}"
        return self.send(text, key=key, reply_markup=markup)

    def send_fill_notification(
        self,
        agent: str,
        symbol: str,
        side: str,
        qty: str,
        price: str,
    ) -> bool:
        """Send a fill notification."""
        text = (
            f"✅ *FILL* \\[{_escape(agent)}\\]\n"
            f"{_escape(side.upper())} {_escape(qty)} "
            f"{_escape(symbol)} @ {_escape(price)}"
        )
        # Fills can be rapid — key includes symbol+side so partial fills dedup separately.
        key = f"fill:{agent}:{symbol}:{side.lower()}"
        return self.send(text, key=key)

    def send_weekly_report(self, report_md: str) -> bool:
        """Send the manager's weekly journal. Deduped once per calendar day."""
        from datetime import UTC, datetime  # noqa: PLC0415

        today = datetime.now(UTC).date().isoformat()
        return self.send(report_md, key=f"weekly:{today}")

    def close(self) -> None:
        """Close the underlying httpx.Client (call once on shutdown)."""
        if self._http is not None:
            with contextlib.suppress(Exception):
                self._http.close()
