"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

import os
from decimal import Decimal


def pytest_configure(config) -> None:  # noqa: ARG001, ANN001
    """Isolate persisted runtime state from the prod data/ directory."""
    # ── Runtime store ──────────────────────────────────────────────────────────
    # Without this, runtime_store.py picks up data/runtime_store.json (whatever
    # the live process most recently wrote) and tests inherit that MC value —
    # e.g. an `MC=0` from a dashboard FREEZE click — which fails any test that
    # expects the planner to size orders against MC=1.0.
    os.environ["RUNTIME_STORE_PATH"] = "/tmp/runtime_store_test.json"
    try:
        os.unlink(os.environ["RUNTIME_STORE_PATH"])
    except FileNotFoundError:
        pass
    try:
        from config import runtime_store as rs  # noqa: PLC0415
        rs._PERSIST_PATH = type(rs._PERSIST_PATH)(os.environ["RUNTIME_STORE_PATH"])
        rs.runtime_store._master_capability = Decimal("1.0")
    except Exception:  # noqa: BLE001
        pass

    # ── Sleeve weights ─────────────────────────────────────────────────────────
    # effective_max_gross() reads data/manager_sleeve_weights.json at test-time.
    # After the first live Manager reallocation (2026-05-28) that file holds real
    # non-1.0 weights (haiku=0.80, sonnet=1.16, opus=1.04) which cause every
    # sizing test that expects base-multiplier outputs to fail. Redirect the path
    # to a temp file that we always write as {} (no weights = base-1.0× for all
    # sleeves). Tests that need non-default weights monkeypatch SLEEVE_WEIGHTS_FILE
    # themselves (e.g. test_manager_reserve.py), which overrides this redirection.
    _test_weights_path = "/tmp/manager_sleeve_weights_test.json"
    try:
        import json  # noqa: PLC0415
        with open(_test_weights_path, "w") as f:
            json.dump({}, f)
    except Exception:  # noqa: BLE001
        pass
    try:
        from pathlib import Path  # noqa: PLC0415
        import agents.manager_bridge as _bridge  # noqa: PLC0415
        _bridge.SLEEVE_WEIGHTS_FILE = Path(_test_weights_path)
    except Exception:  # noqa: BLE001
        pass
