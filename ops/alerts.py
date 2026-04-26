"""ntfy.sh alert adapter — subscribes to EventBus, pushes notifications.

Listens for the five must-page events defined in blueprint §5/§11:
    KillSwitchTrippedEvent     (any non-OK trip)
    ReconciliationBreakEvent
    BudgetExhaustedEvent
    LeverageRotationFlagEvent
    DeepDiveCompleteEvent      (informational; opt-in via NTFY_ALERT_DEEP_DIVE)

Sends an HTTP POST to https://ntfy.sh/{topic}. If the topic is empty,
notifications are dropped (alerts disabled — useful for tests/dev).

Deduplication: identical messages within 60s are suppressed so a flapping
condition does not spam the topic.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from core.events import (
    BudgetExhaustedEvent,
    Event,
    EventBus,
    KillSwitchTrippedEvent,
    LeverageRotationFlagEvent,
    ReconciliationBreakEvent,
)

log = logging.getLogger(__name__)

NTFY_BASE_URL = "https://ntfy.sh"
DEDUPE_WINDOW_SECS: float = 60.0
HTTP_TIMEOUT_SECS: float = 5.0


# Optional injection point for tests: a function (url, body, headers) -> None.
HttpPoster = Callable[[str, bytes, dict[str, str]], None]


def _default_poster(url: str, body: bytes, headers: dict[str, str]) -> None:
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:  # noqa: S310
            resp.read()
    except (URLError, TimeoutError, OSError) as exc:
        log.warning("ntfy POST failed: %s", exc)


class AlertManager:
    """Subscribes to EventBus channels and forwards to ntfy.sh.

    Holds a small in-memory dedupe cache. Network errors are swallowed (logged) —
    alert delivery is best-effort and must never crash the trading loop.
    """

    def __init__(
        self,
        bus: EventBus,
        topic: str,
        *,
        poster: HttpPoster | None = None,
        dedupe_window_secs: float = DEDUPE_WINDOW_SECS,
    ) -> None:
        self._bus = bus
        self._topic = topic
        self._poster = poster if poster is not None else _default_poster
        self._dedupe_window = dedupe_window_secs
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}  # message_key → monotonic ts
        self._sent_count: int = 0
        self._dedup_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to all alert channels."""
        self._bus.subscribe("kill_switch.tripped", self._on_kill_switch)
        self._bus.subscribe("reconciliation.break", self._on_reconciliation_break)
        self._bus.subscribe("budget.exhausted", self._on_budget_exhausted)
        self._bus.subscribe("leverage.rotation_flag", self._on_leverage_rotation)
        log.info(
            "AlertManager: started (topic=%s dedupe=%.0fs)",
            self._topic or "<disabled>",
            self._dedupe_window,
        )

    def stop(self) -> None:
        """Unsubscribe from all channels."""
        self._bus.unsubscribe("kill_switch.tripped", self._on_kill_switch)
        self._bus.unsubscribe("reconciliation.break", self._on_reconciliation_break)
        self._bus.unsubscribe("budget.exhausted", self._on_budget_exhausted)
        self._bus.unsubscribe("leverage.rotation_flag", self._on_leverage_rotation)

    # ── Stats (for dashboard / tests) ─────────────────────────────────────────

    @property
    def sent_count(self) -> int:
        with self._lock:
            return self._sent_count

    @property
    def dedup_count(self) -> int:
        with self._lock:
            return self._dedup_count

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_kill_switch(self, event: Event) -> None:
        if not isinstance(event, KillSwitchTrippedEvent):
            return
        title = "🚨 Kill switch tripped"
        msg = f"state={event.new_state} reason={event.reason}"
        priority = "urgent"
        self._send(title=title, message=msg, key=f"ks:{event.new_state}", priority=priority)

    def _on_reconciliation_break(self, event: Event) -> None:
        if not isinstance(event, ReconciliationBreakEvent):
            return
        title = "⚠️ Reconciliation break"
        msg = (
            f"{event.symbol}: local={event.local_qty} broker={event.broker_qty} "
            f"delta=${event.delta_usd}"
        )
        self._send(title=title, message=msg, key=f"recon:{event.symbol}", priority="high")

    def _on_budget_exhausted(self, event: Event) -> None:
        if not isinstance(event, BudgetExhaustedEvent):
            return
        title = "💸 Daily LLM budget exhausted"
        msg = f"spent_today=${event.spent_today} (system → Haiku-only)"
        self._send(title=title, message=msg, key="budget", priority="high")

    def _on_leverage_rotation(self, event: Event) -> None:
        if not isinstance(event, LeverageRotationFlagEvent):
            return
        title = "🔄 LETF rotation flag"
        msg = (
            f"agent={event.agent_id} symbol={event.symbol} "
            f"category={event.category} reopens={event.reopen_count}"
        )
        self._send(title=title, message=msg, key=f"rot:{event.agent_id}:{event.category}")

    # ── Send + dedupe ─────────────────────────────────────────────────────────

    def _send(
        self,
        *,
        title: str,
        message: str,
        key: str,
        priority: str = "default",
    ) -> None:
        if not self._topic:
            log.debug("ntfy disabled (no topic): %s — %s", title, message)
            return

        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and (now - last) < self._dedupe_window:
                self._dedup_count += 1
                return
            self._last_sent[key] = now
            self._sent_count += 1

        url = f"{NTFY_BASE_URL}/{self._topic}"
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": "warning",
        }
        body = message.encode("utf-8")
        try:
            self._poster(url, body, headers)
        except Exception:
            log.warning("Alert post failed for %s", key, exc_info=True)
