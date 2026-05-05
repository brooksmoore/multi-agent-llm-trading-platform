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
    AgentBenchedEvent,
    BudgetExhaustedEvent,
    DrawdownLadderFiredEvent,
    Event,
    EventBus,
    KillSwitchResetEvent,
    KillSwitchTrippedEvent,
    LeverageRotationFlagEvent,
    ReconciliationBreakEvent,
)
from ops.telegram import TelegramAdapter

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
        telegram: TelegramAdapter | None = None,
    ) -> None:
        self._bus = bus
        self._topic = topic
        self._poster = poster if poster is not None else _default_poster
        self._dedupe_window = dedupe_window_secs
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}  # message_key → monotonic ts
        self._sent_count: int = 0
        self._dedup_count: int = 0
        self._telegram = telegram

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to all alert channels."""
        self._bus.subscribe("kill_switch.tripped", self._on_kill_switch)
        self._bus.subscribe("kill_switch.reset", self._on_kill_switch_reset)
        self._bus.subscribe("reconciliation.break", self._on_reconciliation_break)
        self._bus.subscribe("budget.exhausted", self._on_budget_exhausted)
        self._bus.subscribe("leverage.rotation_flag", self._on_leverage_rotation)
        self._bus.subscribe("agent.benched", self._on_agent_benched)
        self._bus.subscribe("drawdown.ladder_fired", self._on_drawdown_ladder)
        log.info(
            "AlertManager: started (topic=%s telegram=%s dedupe=%.0fs)",
            self._topic or "<disabled>",
            "enabled" if (self._telegram is not None and self._telegram.enabled) else "disabled",
            self._dedupe_window,
        )

    def stop(self) -> None:
        """Unsubscribe from all channels."""
        self._bus.unsubscribe("kill_switch.tripped", self._on_kill_switch)
        self._bus.unsubscribe("kill_switch.reset", self._on_kill_switch_reset)
        self._bus.unsubscribe("reconciliation.break", self._on_reconciliation_break)
        self._bus.unsubscribe("budget.exhausted", self._on_budget_exhausted)
        self._bus.unsubscribe("leverage.rotation_flag", self._on_leverage_rotation)
        self._bus.unsubscribe("agent.benched", self._on_agent_benched)
        self._bus.unsubscribe("drawdown.ladder_fired", self._on_drawdown_ladder)

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
        self._send(title=title, message=msg, key=f"ks:{event.new_state}", priority="urgent")
        if self._telegram is not None:
            self._telegram.send_halt_alert(
                reason=f"{event.new_state} — {event.reason}",
                agent=str(event.agent_id) if event.agent_id else None,
            )

    def _on_reconciliation_break(self, event: Event) -> None:
        if not isinstance(event, ReconciliationBreakEvent):
            return
        title = "⚠️ Reconciliation break"
        msg = (
            f"{event.symbol}: local={event.local_qty} broker={event.broker_qty} "
            f"delta=${event.delta_usd}"
        )
        self._send(title=title, message=msg, key=f"recon:{event.symbol}", priority="high")
        if self._telegram is not None:
            self._telegram.send_halt_alert(
                reason=f"Reconciliation break {event.symbol}: delta=${event.delta_usd}",
            )

    def _on_budget_exhausted(self, event: Event) -> None:
        if not isinstance(event, BudgetExhaustedEvent):
            return
        title = "💸 Daily LLM budget exhausted"
        msg = f"spent_today=${event.spent_today} (system → Haiku-only)"
        self._send(title=title, message=msg, key="budget", priority="high")
        if self._telegram is not None:
            self._telegram.send_halt_alert(
                reason=f"Daily LLM budget exhausted — spent ${event.spent_today}",
                agent=str(event.agent_id) if event.agent_id else None,
            )

    def _on_leverage_rotation(self, event: Event) -> None:
        if not isinstance(event, LeverageRotationFlagEvent):
            return
        title = "🔄 LETF rotation flag"
        msg = (
            f"agent={event.agent_id} symbol={event.symbol} "
            f"category={event.category} reopens={event.reopen_count}"
        )
        self._send(title=title, message=msg, key=f"rot:{event.agent_id}:{event.category}")

    def _on_agent_benched(self, event: Event) -> None:
        if not isinstance(event, AgentBenchedEvent):
            return
        title = "🪑 Agent benched"
        msg = (
            f"agent={event.agent_id} after {event.consecutive_losses} "
            "consecutive losses (24h cooldown)"
        )
        self._send(title=title, message=msg, key=f"bench:{event.agent_id}", priority="high")

    def _on_drawdown_ladder(self, event: Event) -> None:
        if not isinstance(event, DrawdownLadderFiredEvent):
            return
        # Only alert on tightening to ORANGE/RED/FORCED_CASH (the buckets
        # that meaningfully throttle the agent). YELLOW is routine noise.
        if event.new_bucket in ("normal", "yellow", ""):
            return
        emoji = "🟠" if event.new_bucket == "orange" else "🔴"
        title = f"{emoji} Drawdown bucket {event.new_bucket}"
        msg = (
            f"agent={event.agent_id} drawdown={float(event.drawdown_pct) * 100:.1f}% "
            f"(sizing now scaled by drawdown ladder)"
        )
        self._send(
            title=title, message=msg,
            key=f"dd:{event.agent_id}:{event.new_bucket}",
            priority="urgent" if event.new_bucket in ("red", "forced_cash") else "high",
        )

    def _on_kill_switch_reset(self, event: Event) -> None:
        if not isinstance(event, KillSwitchResetEvent):
            return
        title = "✅ Kill switch cleared"
        msg = "Trading resumed"
        self._send(title=title, message=msg, key="ks:reset", priority="default")
        if self._telegram is not None:
            self._telegram.send_resume_alert(
                agent=str(event.agent_id) if event.agent_id else None,
            )

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
