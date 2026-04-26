"""Dashboard app — Plotly Dash on :8081, polls every 3s, read-only.

Per blueprint Principle 9: "the dashboard is read-only. It polls SQLite/DuckDB
every 3s; it never mutates state and is not on the trading code path."

Usage:
    DASHBOARD_PORT=8081 \
    OMS_DB=data/oms.db \
    BUDGET_PATH=data/daily_spend.json \
    AGENT_MEMORY_DB=data/agent_memory.db \
    CALIBRATION_DB=data/calibration.db \
    python -m dashboard.app
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from pathlib import Path

from dash import Dash, Input, Output, dcc, html

from agents.calibration import CalibrationTracker
from agents.memory import AgentMemory
from core.types import AgentId
from dashboard.data import DashboardData
from dashboard.layout import render_full_dashboard
from execution.budget import BudgetLedger
from execution.oms_store import OMSStore

log = logging.getLogger(__name__)

POLL_INTERVAL_MS = 3000


def build_app(data: DashboardData) -> Dash:
    """Wire a Dash app around an injected DashboardData (testable)."""
    app = Dash(__name__, title="Multi-Agent Bot")
    app.layout = html.Div([
        dcc.Interval(id="tick", interval=POLL_INTERVAL_MS, n_intervals=0),
        html.Div(id="root"),
    ])

    @app.callback(Output("root", "children"), Input("tick", "n_intervals"))
    def _refresh(_n: int) -> html.Div:
        return render_full_dashboard(data)

    return app


def _load_from_env() -> DashboardData:
    """Open read-only handles to the configured stores."""
    oms_path = os.environ.get("OMS_DB")
    budget_path = os.environ.get("BUDGET_PATH", "data/daily_spend.json")
    memory_path = os.environ.get("AGENT_MEMORY_DB", "data/agent_memory.db")
    calibration_path = os.environ.get("CALIBRATION_DB", "data/calibration.db")

    oms = OMSStore(oms_path) if oms_path and Path(oms_path).exists() else None
    budget = BudgetLedger(Path(budget_path)) if Path(budget_path).exists() else None
    calibration = (
        CalibrationTracker(calibration_path) if Path(calibration_path).exists() else None
    )
    memories = {
        agent_id: AgentMemory(memory_path, agent_id)
        for agent_id in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS, AgentId.MANAGER)
        if Path(memory_path).exists()
    }
    return DashboardData(
        oms_store=oms,
        memories=memories,
        calibration=calibration,
        budget=budget,
        master_capability=Decimal(os.environ.get("MASTER_CAPABILITY", "1.0")),
        regime_label=os.environ.get("CURRENT_REGIME", "unknown"),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("DASHBOARD_PORT", "8081"))
    data = _load_from_env()
    app = build_app(data)
    log.info("dashboard starting on http://localhost:%d", port)
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
