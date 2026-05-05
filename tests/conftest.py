"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

import os
from decimal import Decimal


def pytest_configure(config) -> None:  # noqa: ARG001, ANN001
    """Isolate persisted runtime state from the prod data/ directory.

    Without this, runtime_store.py picks up data/runtime_store.json (whatever
    the live process most recently wrote) and tests inherit that MC value —
    e.g. an `MC=0` from a dashboard FREEZE click — which fails any test that
    expects the planner to size orders against MC=1.0.
    """
    os.environ["RUNTIME_STORE_PATH"] = "/tmp/runtime_store_test.json"
    # Force a clean slate every run.
    try:
        os.unlink(os.environ["RUNTIME_STORE_PATH"])
    except FileNotFoundError:
        pass

    # Reset any already-imported module-level singleton to the test-isolated
    # default MC value. Tests imported after this point pick up the new path.
    try:
        from config import runtime_store as rs  # noqa: PLC0415
        rs._PERSIST_PATH = type(rs._PERSIST_PATH)(os.environ["RUNTIME_STORE_PATH"])
        rs.runtime_store._master_capability = Decimal("1.0")
    except Exception:  # noqa: BLE001
        pass
