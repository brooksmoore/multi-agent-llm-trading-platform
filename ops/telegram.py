"""Telegram notification adapter — STUB for v1.5.

Full implementation deferred to milestone 7 (post-milestone-6 agent wiring).
The real adapter will use the Telegram Bot API via httpx.

This stub provides the same interface so the rest of the system can
import and call it without error during v1 development.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TelegramAdapter:
    """Stub Telegram notification adapter.

    All methods are no-ops in v1. The interface matches what ops/alerts.py
    will call, so swapping in the real implementation in v1.5 requires zero
    changes to callers.
    """

    def __init__(self, bot_token: str = "", chat_id: str = "") -> None:
        self._enabled = bool(bot_token and chat_id)
        if not self._enabled:
            logger.debug("TelegramAdapter: no credentials — stub mode, all sends are no-ops")

    async def send(self, message: str) -> bool:
        """Send a text message. Returns True on success, False on failure."""
        if not self._enabled:
            logger.debug("TelegramAdapter.send() [stub]: %s", message[:80])
            return False
        # v1.5: POST https://api.telegram.org/bot{token}/sendMessage
        # with json={"chat_id": self._chat_id, "text": message, "parse_mode": "Markdown"}
        logger.warning("TelegramAdapter.send() called but not yet implemented (v1.5)")
        return False

    async def send_halt_alert(self, reason: str, agent: str | None = None) -> bool:
        tag = f"[{agent}] " if agent else ""
        return await self.send(f"🚨 HALT — {tag}{reason}")

    async def send_fill_notification(
        self,
        agent: str,
        symbol: str,
        side: str,
        qty: str,
        price: str,
    ) -> bool:
        return await self.send(
            f"✅ FILL [{agent}] {side.upper()} {qty} {symbol} @ {price}"
        )

    async def send_weekly_report(self, report_md: str) -> bool:
        # Telegram has a 4096-char message limit; truncate gracefully
        truncated = report_md[:4000] + "…" if len(report_md) > 4000 else report_md
        return await self.send(truncated)
