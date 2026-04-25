"""Pending intent approval queue (used when AUTO_APPROVE=False).

When human approval is required, intents are enqueued here before reaching
the OMS. The dashboard polls pending() and the human approves/rejects via
the UI. Intents that aren't acted on within APPROVAL_EXPIRY_HOURS are
auto-rejected.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.types import Intent, IntentId

APPROVAL_EXPIRY_HOURS: int = 4


@dataclass
class PendingIntent:
    intent: Intent
    received_at: datetime
    expires_at: datetime
    approved: bool = field(default=False)
    rejected: bool = field(default=False)

    @property
    def is_pending(self) -> bool:
        return not self.approved and not self.rejected

    @property
    def is_expired(self) -> bool:
        """True if the intent was never acted on and its window has closed."""
        return self.rejected and not self.approved


class ApprovalQueue:
    """Thread-safe queue of intents awaiting human approval."""

    def __init__(self, expiry_hours: int = APPROVAL_EXPIRY_HOURS) -> None:
        self._lock = threading.Lock()
        self._queue: dict[IntentId, PendingIntent] = {}
        self._expiry_hours = expiry_hours

    def enqueue(self, intent: Intent, ts: datetime) -> PendingIntent:
        """Add an intent to the approval queue. Returns the PendingIntent wrapper."""
        pending = PendingIntent(
            intent=intent,
            received_at=ts,
            expires_at=ts + timedelta(hours=self._expiry_hours),
        )
        with self._lock:
            self._queue[intent.id] = pending
        return pending

    def pending(self) -> list[PendingIntent]:
        """Return all intents that are still waiting for a decision."""
        with self._lock:
            return [p for p in self._queue.values() if p.is_pending]

    def approve(self, intent_id: IntentId, ts: datetime) -> Intent | None:
        """Approve an intent. Returns the Intent if approved, None if not found or expired."""
        with self._lock:
            p = self._queue.get(intent_id)
            if p is None or not p.is_pending:
                return None
            if ts > p.expires_at:
                p.rejected = True
                return None
            p.approved = True
            return p.intent

    def reject(self, intent_id: IntentId) -> bool:
        """Reject an intent. Returns True if found and rejected."""
        with self._lock:
            p = self._queue.get(intent_id)
            if p is None or not p.is_pending:
                return False
            p.rejected = True
            return True

    def expire_old(self, ts: datetime) -> list[PendingIntent]:
        """Auto-reject all intents past their expiry. Returns the expired ones."""
        expired: list[PendingIntent] = []
        with self._lock:
            for p in self._queue.values():
                if p.is_pending and ts > p.expires_at:
                    p.rejected = True
                    expired.append(p)
        return expired

    def all(self) -> list[PendingIntent]:
        with self._lock:
            return list(self._queue.values())
