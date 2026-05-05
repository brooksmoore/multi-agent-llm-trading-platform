"""Thread-safe runtime settings store.

Holds mutable runtime values that can change without a process restart.
The dashboard MC slider writes here; app.py reads master_capability before
each agent dispatch to pass to execution/sizing.compute_effective_max_gross().

Persists master_capability to a small JSON file so a process restart
doesn't silently revert a value the operator set.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from decimal import Decimal
from pathlib import Path

from config.settings import settings

log = logging.getLogger(__name__)

MAX_MASTER_CAPABILITY = Decimal("1.5")
# Tests override via RUNTIME_STORE_PATH to avoid touching the prod file.
_PERSIST_PATH = Path(os.environ.get("RUNTIME_STORE_PATH", "data/runtime_store.json"))


def _load_persisted_mc(default: Decimal) -> Decimal:
    try:
        raw = json.loads(_PERSIST_PATH.read_text())
        return Decimal(str(raw["master_capability"]))
    except FileNotFoundError:
        return default
    except Exception:
        log.warning("runtime_store: failed to read %s; using default", _PERSIST_PATH, exc_info=True)
        return default


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
        self._persist(value)

    def _persist(self, value: Decimal) -> None:
        try:
            _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            _PERSIST_PATH.write_text(json.dumps({"master_capability": str(value)}))
        except Exception:
            log.warning("runtime_store: failed to persist to %s", _PERSIST_PATH, exc_info=True)


runtime_store: RuntimeStore = RuntimeStore(
    initial_mc=_load_persisted_mc(settings.master_capability)
)
