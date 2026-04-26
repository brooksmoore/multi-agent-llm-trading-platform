"""Thread-safe runtime settings store.

Holds mutable runtime values that can change without a process restart.
The dashboard MC slider writes here; app.py reads master_capability before
each agent dispatch to pass to execution/sizing.compute_effective_max_gross().
"""

from __future__ import annotations

import logging
import os
import threading
from decimal import Decimal

from config.settings import settings

log = logging.getLogger(__name__)

MAX_MASTER_CAPABILITY = Decimal("1.5")


class RuntimeStore:
    """Thread-safe container for runtime-mutable settings."""

    def __init__(self, initial_mc: Decimal = Decimal("1.0")) -> None:
        self._lock = threading.Lock()
        self._master_capability: Decimal = initial_mc

    @property
    def master_capability(self) -> Decimal:
        with self._lock:
            return self._master_capability

    @master_capability.setter
    def master_capability(self, value: Decimal) -> None:
        with self._lock:
            if value > MAX_MASTER_CAPABILITY and not os.environ.get("OVERRIDE_KEY"):
                log.warning(
                    "MASTER_CAPABILITY %.2f exceeds max %.2f without OVERRIDE_KEY — "
                    "clamping to %.2f",
                    float(value),
                    float(MAX_MASTER_CAPABILITY),
                    float(MAX_MASTER_CAPABILITY),
                )
                value = MAX_MASTER_CAPABILITY
            self._master_capability = value


runtime_store: RuntimeStore = RuntimeStore(initial_mc=settings.master_capability)
