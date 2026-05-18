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
from decimal import Decimal
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

# Reconciliation-break notification policy. The kill switch trips immediately
# (trading halts), but Telegram/ntfy is deferred to filter out transient races
# (broker-fill webhook lag, intra-tick OMS writes) that auto-clear within a few
# reconcile cycles. A material dollar delta bypasses the grace period.
RECON_GRACE_SECS: float = 180.0
RECON_MATERIAL_DELTA_USD: Decimal = Decimal("25")

# How long a kill switch must remain tripped before we page Telegram. Below
# this threshold the trip is assumed to be a transient (recon race, broker
# hiccup) and rolls into the daily recap instead. Set conservatively: most
# auto-resolving halts clear in well under 5 min.
HALT_TELEGRAM_GRACE_SECS: float = 30 * 60.0


# Optional injection point for tests: a function (url, body, headers) -> None.
HttpPoster = Callable[[str, bytes, dict[str, str]], None]


def _ascii_header(value: str) -> str:
    # HTTP header values must be latin-1 encodable. ntfy Title/Tags often
    # contain emoji — strip anything non-ASCII to avoid UnicodeEncodeError
    # inside urllib.
    return value.encode("ascii", "ignore").decode("ascii").strip() or "alert"


def _default_poster(url: str, body: bytes, headers: dict[str, str]) -> None:
    safe_headers = {k: _ascii_header(v) for k, v in headers.items()}
    req = Request(url, data=body, headers=safe_headers, method="POST")
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
        recon_grace_secs: float = RECON_GRACE_SECS,
        recon_material_delta_usd: Decimal = RECON_MATERIAL_DELTA_USD,
        halt_telegram_grace_secs: float = HALT_TELEGRAM_GRACE_SECS,
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
        self._recon_grace = recon_grace_secs
        self._recon_material = recon_material_delta_usd
        # Per-symbol deferred-notify timers. Cancelled if a kill-switch reset
        # arrives before the grace period expires (i.e. the break self-cleared).
        self._pending_recon: dict[str, threading.Timer] = {}
        self._recon_suppressed_count: int = 0
        self._halt_grace = halt_telegram_grace_secs
        # Single in-flight stuck-halt timer. We Telegram only if the kill
        # switch stays tripped past the grace window; otherwise the trip is
        # recorded in the daily recap and not paged.
        self._pending_halt: threading.Timer | None = None
        self._halt_suppressed_count: int = 0

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
        # Cancel any in-flight deferred timers.
        with self._lock:
            pending = list(self._pending_recon.values())
            self._pending_recon.clear()
            pending_halt = self._pending_halt
            self._pending_halt = None
        for timer in pending:
            timer.cancel()
        if pending_halt is not None:
            pending_halt.cancel()

    # ── Stats (for dashboard / tests) ─────────────────────────────────────────

    @property
    def sent_count(self) -> int:
        with self._lock:
            return self._sent_count

    @property
    def dedup_count(self) -> int:
        with self._lock:
            return self._dedup_count

    @property
    def recon_suppressed_count(self) -> int:
        """Reconciliation-break alerts that were deferred and then cancelled
        because the break auto-cleared within the grace window."""
        with self._lock:
            return self._recon_suppressed_count

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_kill_switch(self, event: Event) -> None:
        if not isinstance(event, KillSwitchTrippedEvent):
            return
        title = "🚨 Kill switch tripped"
        msg = f"state={event.new_state} reason={event.reason}"
        # ntfy (the always-on, low-friction channel) fires immediately.
        self._send(title=title, message=msg, key=f"ks:{event.new_state}", priority="urgent")
        # Telegram is reserved for *stuck* halts: defer by grace window; if a
        # reset arrives first the timer is cancelled. Routine transient trips
        # roll into the daily recap, not real-time push.
        if self._telegram is None:
            return
        with self._lock:
            if self._pending_halt is not None:
                self._pending_halt.cancel()
            agent_str = str(event.agent_id) if event.agent_id else None
            timer = threading.Timer(
                self._halt_grace,
                self._fire_stuck_halt_alert,
                args=(event.new_state, event.reason, agent_str),
            )
            timer.daemon = True
            self._pending_halt = timer
        timer.start()
        log.info(
            "AlertManager: kill switch tripped (state=%s); Telegram deferred %.0fs",
            event.new_state, self._halt_grace,
        )

    def _fire_stuck_halt_alert(
        self, state: str, reason: str, agent: str | None,
    ) -> None:
        with self._lock:
            self._pending_halt = None
        if self._telegram is None:
            return
        self._telegram.send_halt_alert(
            reason=f"Stuck halt (>{int(self._halt_grace / 60)}m): {state} — {reason}",
            agent=agent,
        )

    def _on_reconciliation_break(self, event: Event) -> None:
        if not isinstance(event, ReconciliationBreakEvent):
            return
        # Material breaks (delta ≥ threshold) page immediately — they're large
        # enough that even a transient race is worth knowing about.
        material = abs(event.delta_usd) >= self._recon_material
        if material:
            self._fire_recon_alert(event)
            return
        # Otherwise, defer the notification by `grace` seconds. If the break
        # auto-clears (KillSwitchResetEvent), we cancel the timer below and
        # never page. Persistent breaks survive the grace period and page.
        with self._lock:
            existing = self._pending_recon.pop(event.symbol, None)
        if existing is not None:
            existing.cancel()
        timer = threading.Timer(
            self._recon_grace, self._fire_recon_alert, args=(event,)
        )
        timer.daemon = True
        with self._lock:
            self._pending_recon[event.symbol] = timer
        timer.start()
        log.info(
            "AlertManager: reconciliation break on %s deferred %.0fs "
            "(local=%s broker=%s delta=$%s)",
            event.symbol, self._recon_grace,
            event.local_qty, event.broker_qty, event.delta_usd,
        )

    def _fire_recon_alert(self, event: ReconciliationBreakEvent) -> None:
        with self._lock:
            # Drop our own pending entry if this is the deferred path.
            self._pending_recon.pop(event.symbol, None)
        title = "⚠️ Reconciliation break"
        msg = (
            f"{event.symbol}: local={event.local_qty} broker={event.broker_qty} "
            f"delta=${event.delta_usd}"
        )
        # ntfy only — Telegram intentionally not sent. Recon breaks are
        # auto-handled (kill switch + reconciler retry); the count surfaces
        # in the daily recap.
        self._send(title=title, message=msg, key=f"recon:{event.symbol}", priority="high")

    def _on_budget_exhausted(self, event: Event) -> None:
        if not isinstance(event, BudgetExhaustedEvent):
            return
        title = "💸 Daily LLM budget exhausted"
        msg = f"spent_today=${event.spent_today} (system → Haiku-only)"
        # ntfy only — budget exhaustion is recurring and bounded (system falls
        # back to Haiku-only); details surface in the daily recap.
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
        # Cancel any in-flight deferred timers — the halt resolved within
        # the grace window, so by policy these never page Telegram.
        with self._lock:
            pending_recon = list(self._pending_recon.items())
            self._pending_recon.clear()
            self._recon_suppressed_count += len(pending_recon)
            pending_halt = self._pending_halt
            self._pending_halt = None
            if pending_halt is not None:
                self._halt_suppressed_count += 1
        for symbol, timer in pending_recon:
            timer.cancel()
            log.debug(
                "AlertManager: reconciliation break on %s auto-cleared", symbol,
            )
        if pending_halt is not None:
            pending_halt.cancel()
            log.info("AlertManager: stuck-halt timer cancelled (kill switch cleared)")
        # ntfy still gets the resume (low-friction); Telegram does not — by
        # design, no resume push. The daily recap reports halt counts.
        title = "✅ Kill switch cleared"
        msg = "Trading resumed"
        self._send(title=title, message=msg, key="ks:reset", priority="default")

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
