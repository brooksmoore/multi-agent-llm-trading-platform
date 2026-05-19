"""Flask dashboard — single-file HTML/CSS/JS, replaces the Dash UI on :8081.

Read-only against the bot stores. The only write path is POST /api/master_capability,
which mirrors the old Dash MC preset buttons. Modeled after the kalshi_bot_2.0
dashboard: Flask + Chart.js + 15s polling, no framework bloat.
"""

from __future__ import annotations

import logging
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask, jsonify, render_template_string, request  # noqa: E402

from agents.calibration import CalibrationTracker  # noqa: E402
from agents.memory import AgentMemory  # noqa: E402
from config.runtime_store import runtime_store  # noqa: E402
from core.types import AgentId  # noqa: E402
from dashboard.data import DashboardData  # noqa: E402
from dashboard.spy import SPYProvider  # noqa: E402
from execution.budget import BudgetLedger  # noqa: E402
from execution.oms_store import OMSStore  # noqa: E402

log = logging.getLogger(__name__)


def _d(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


def build_app(data: DashboardData, spy: SPYProvider | None = None) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index() -> str:
        return render_template_string(_HTML)

    @app.route("/api/snapshot")
    def snapshot() -> object:
        ts = data.top_strip()
        agents_payload = []
        for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS, AgentId.MANAGER):
            s = data.agent_summary(aid, n_intents=3)
            intents = [
                {
                    "ts": i.timestamp,
                    "symbol": i.symbol,
                    "action": i.action,
                    "conviction": i.conviction,
                    "outcome": i.outcome,
                    "rationale": i.rationale,
                }
                for i in s.recent_intents
            ]
            short = s.agent_id.split(".")[-1].lower() if "." in s.agent_id else s.agent_id.lower()
            perf = data.agent_performance(short)
            agents_payload.append(
                {
                    "agent_id": s.agent_id,
                    "model": s.model,
                    "sleeve_equity": _d(s.sleeve_equity),
                    "four_week_return_pct": _d(s.four_week_return_pct),
                    "brier_score": s.brier_score,
                    "intents": intents,
                    "performance": {
                        "sharpe_4w": perf.sharpe_4w,
                        "sortino_4w": perf.sortino_4w,
                        "max_dd_4w": perf.max_dd_4w,
                        "win_rate": perf.win_rate,
                        "loss_rate": perf.loss_rate,
                        "n_closed": perf.n_closed,
                    },
                }
            )

        # Sleeve equity & drawdowns from snapshot DB rolled into agents_payload
        latest_eq: dict[str, float] = {}
        for p in data.sleeve_curves():
            latest_eq[p.agent_id] = float(p.equity)
        # better: pull peaks directly
        for ap in agents_payload:
            short = ap["agent_id"].split(".")[-1].lower() if "." in ap["agent_id"] else ap["agent_id"].lower()
            ap["sleeve_equity_live"] = latest_eq.get(short)

        for dd in data.drawdown_status():
            for ap in agents_payload:
                short = ap["agent_id"].split(".")[-1].lower() if "." in ap["agent_id"] else ap["agent_id"].lower()
                if short == dd.agent_id:
                    ap["drawdown_pct"] = dd.drawdown_pct
                    ap["drawdown_bucket"] = dd.bucket

        positions = [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": p.side,
                "market_value": float(p.market_value),
                "unrealized_pl": _d(p.unrealized_pl),
            }
            for p in data.current_positions()
        ]

        def _pos_payload(p: Any) -> dict[str, Any]:
            return {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": p.side,
                "market_value": float(p.market_value),
                "unrealized_pl": _d(p.unrealized_pl),
            }

        positions_by_agent = {
            agent: [_pos_payload(p) for p in rows]
            for agent, rows in data.current_positions_by_agent().items()
        }

        spend = data.spend_breakdown()

        return jsonify(
            {
                "top": {
                    "total_nav": _d(ts.total_nav),
                    "day_pnl": _d(ts.day_pnl_gross),
                    "day_spend": float(ts.day_spend_usd),
                    "spend_limit": float(ts.spend_limit_usd),
                    "spend_pct": ts.spend_pct,
                    "halted": ts.halted,
                    "master_capability": float(ts.master_capability),
                    "effective_max_gross": float(ts.effective_max_gross),
                    "regime": ts.regime_label,
                    "vix_bucket": ts.vix_bucket,
                    "heartbeat_age_s": ts.heartbeat_age_s,
                },
                "agents": agents_payload,
                "positions": positions,
                "positions_by_agent": positions_by_agent,
                "spend": {
                    "today_total": float(spend.today_total),
                    "daily_limit": float(spend.daily_limit),
                    "by_agent": {k: float(v) for k, v in spend.by_agent.items()},
                    "by_call_type": {k: float(v) for k, v in spend.by_call_type.items()},
                    "eod_forecast": float(spend.eod_forecast),
                },
            }
        )

    @app.route("/api/nav_curve")
    def nav_curve() -> object:
        nav = [
            {"ts": p.ts, "nav": _d(p.total_nav)}
            for p in data.nav_curve()
            if p.total_nav is not None
        ]
        spy_closes = spy.daily_closes(days=60) if spy is not None else []
        spy_series: list[dict[str, object]] = []
        nav_today_return: float | None = None
        spy_today_return: float | None = None
        if nav and spy_closes:
            # Trim SPY to dates >= the first NAV date so the chart begins on
            # our first trading day (not weeks of SPY history before launch).
            first_nav_date = str(nav[0]["ts"])[:10]
            spy_in_range = [(d, c) for (d, c) in spy_closes if d >= first_nav_date]
            if spy_in_range:
                # Anchor first in-range SPY close to the first NAV value.
                anchor_nav = nav[0]["nav"]
                spy_anchor = float(spy_in_range[0][1])
                if spy_anchor:
                    for date_iso, close in spy_in_range:
                        spy_series.append(
                            {"ts": date_iso, "value": (float(close) / spy_anchor) * float(anchor_nav)}
                        )
            # SPY return today: last close vs prior close from the FULL series
            # (so we always have a prior day, even if it predates first NAV).
            if len(spy_closes) >= 2:
                prev_close = float(spy_closes[-2][1])
                last_close = float(spy_closes[-1][1])
                if prev_close:
                    spy_today_return = (last_close - prev_close) / prev_close
        # NAV return today: first vs last NAV point sharing the latest date.
        if nav:
            today_date = str(nav[-1]["ts"])[:10]
            today_pts = [p for p in nav if str(p["ts"])[:10] == today_date]
            if len(today_pts) >= 2:
                start = float(today_pts[0]["nav"])
                end = float(today_pts[-1]["nav"])
                if start:
                    nav_today_return = (end - start) / start
            elif len(today_pts) == 1:
                nav_today_return = 0.0
        return jsonify({
            "nav": nav,
            "spy": spy_series,
            "nav_today_return": nav_today_return,
            "spy_today_return": spy_today_return,
        })

    @app.route("/api/daily_vs_spy")
    def daily_vs_spy() -> object:
        """One row per trading day: bot's daily % return vs SPY's daily % return.

        Daily % is computed from the LAST equity_snapshot NAV per calendar
        date (close-of-day proxy) and SPY's daily close from Alpaca's
        AlpacaMarketData. Only dates with BOTH a NAV close and a SPY close
        are emitted, so weekends/holidays drop out automatically (SPY
        doesn't trade then) and crypto-only nav drift doesn't get falsely
        compared to nothing.
        """
        nav_closes = data.daily_nav_closes()
        spy_closes = spy.daily_closes(days=90) if spy is not None else []
        spy_by_date = {d: float(c) for d, c in spy_closes}

        out: list[dict[str, object]] = []
        prev_nav: float | None = None
        prev_spy: float | None = None
        # Iterate in date order. Skip dates lacking a SPY close.
        for pt in nav_closes:
            if pt.date not in spy_by_date:
                continue
            nav_val = float(pt.nav)
            spy_val = spy_by_date[pt.date]
            if prev_nav is not None and prev_nav > 0 and prev_spy:
                nav_pct = (nav_val - prev_nav) / prev_nav
                spy_pct = (spy_val - prev_spy) / prev_spy
                out.append({
                    "date": pt.date,
                    "nav_pct": nav_pct,
                    "spy_pct": spy_pct,
                    "alpha_pct": nav_pct - spy_pct,
                })
            prev_nav = nav_val
            prev_spy = spy_val
        return jsonify({"daily": out})

    @app.route("/api/sleeve_curves")
    def sleeve_curves() -> object:
        by_agent: dict[str, list[dict[str, float | str]]] = {}
        for p in data.sleeve_curves():
            by_agent.setdefault(p.agent_id, []).append({"ts": p.ts, "equity": float(p.equity)})
        return jsonify(by_agent)

    @app.route("/api/activity")
    def activity() -> object:
        merged: list[dict[str, object]] = []
        for f in data.recent_fills(50):
            merged.append(
                {
                    "kind": "fill",
                    "ts": f.timestamp,
                    "agent": f.agent_id,
                    "symbol": f.symbol,
                    "action": f.side,
                    "qty": float(f.qty),
                    "price": float(f.price),
                    "total_cost": float(f.total_cost),
                    "rationale": f.rationale,
                }
            )
        for i in data.recent_intents(50):
            merged.append(
                {
                    "kind": "intent",
                    "ts": i.timestamp,
                    "agent": i.agent_id,
                    "symbol": i.symbol,
                    "action": i.action,
                    "conviction": i.conviction,
                    "outcome": i.outcome,
                    "rationale": i.rationale,
                }
            )
        merged.sort(key=lambda r: str(r["ts"]), reverse=True)
        return jsonify(merged[:80])

    @app.route("/api/calibration")
    def calibration() -> object:
        return jsonify(
            [
                {
                    "agent": p.agent_id,
                    "conviction": p.conviction_bucket,
                    "win_rate": p.win_rate,
                    "n": p.n,
                }
                for p in data.calibration_scatter()
            ]
        )

    @app.route("/api/spend_curve")
    def spend_curve() -> object:
        return jsonify(
            [{"ts": p.ts, "cumulative": float(p.cumulative_usd)} for p in data.spend_curve()]
        )

    @app.route("/api/agent_pnl")
    def agent_pnl() -> object:
        """Per-sleeve P&L attribution snapshots, newest first (T1.5)."""
        try:
            limit = int(request.args.get("limit", "10"))
        except ValueError:
            limit = 10
        return jsonify(data.agent_pnl_recent(limit=max(1, min(limit, 60))))

    @app.route("/api/master_capability", methods=["POST"])
    def set_mc() -> object:
        body = request.get_json(silent=True) or {}
        try:
            chosen = Decimal(str(body.get("value")))
        except Exception:
            return jsonify({"error": "invalid value"}), 400
        runtime_store.master_capability = chosen
        return jsonify({"value": float(runtime_store.master_capability)})

    return app


def _load_from_env() -> tuple[DashboardData, SPYProvider]:
    oms_path = os.environ.get("OMS_DB")
    budget_path = os.environ.get("BUDGET_PATH", "data/daily_spend.json")
    memory_path = os.environ.get("AGENT_MEMORY_DB", "data/agent_memory.db")
    calibration_path = os.environ.get("CALIBRATION_DB", "data/calibration.db")
    snapshot_path = os.environ.get("SNAPSHOT_DB", "data/equity_snapshots.db")
    lots_path = os.environ.get("LOTS_DB", "data/lots.db")

    oms = OMSStore(oms_path) if oms_path and Path(oms_path).exists() else None
    budget = BudgetLedger(Path(budget_path)) if Path(budget_path).exists() else None
    cal = CalibrationTracker(calibration_path) if Path(calibration_path).exists() else None
    memories = {
        aid: AgentMemory(memory_path, aid)
        for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS, AgentId.MANAGER)
        if Path(memory_path).exists()
    }
    data = DashboardData(
        oms_store=oms,
        memories=memories,
        calibration=cal,
        budget=budget,
        regime_label=os.environ.get("CURRENT_REGIME", "unknown"),
        snapshot_db_path=Path(snapshot_path) if snapshot_path else None,
        lots_db_path=Path(lots_path) if lots_path and Path(lots_path).exists() else None,
    )
    return data, SPYProvider()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Silence per-request 200 OK access logs from the Flask dev server; keep
    # warnings/errors visible so real failures still surface.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    port = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or "8081")
    data, spy = _load_from_env()
    app = build_app(data, spy)
    log.info("dashboard starting on http://localhost:%d", port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


# ─── HTML / CSS / JS ──────────────────────────────────────────────────────────

_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Multi-Agent Bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/date-fns@3.0.6/cdn.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root {
    --bg: #0b0d12;
    --panel: #11141b;
    --panel-2: #161a23;
    --border: #242a3a;
    --fg: #e6e8ee;
    --dim: #8891a3;
    --label: #6b7385;
    --accent: #79c0ff;
    --good: #7ee787;
    --warn: #d29922;
    --bad: #ff7b72;
    --buy: #7ee787;
    --sell: #ff7b72;
    --haiku: #79c0ff;
    --sonnet: #d2a8ff;
    --opus: #ffa657;
    --manager: #7ee787;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
  }
  .mono, .num { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-variant-numeric: tabular-nums; }
  .wrap { max-width: 1480px; margin: 0 auto; padding: 18px 22px 60px; }
  header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 18px; }
  h1 { font-size: 18px; font-weight: 600; letter-spacing: 0.02em; margin: 0; }
  .updated { color: var(--dim); font-size: 12px; }
  .label { color: var(--label); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 600; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .grid { display: grid; gap: 14px; }

  /* Hero band */
  .hero { grid-template-columns: 1.6fr 1fr 1fr 1fr; align-items: stretch; }
  .hero .big { font-size: 28px; font-weight: 600; margin-top: 4px; }
  .hero .sub { font-size: 12px; color: var(--dim); margin-top: 2px; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; }
  .pill.live { background: #173023; color: var(--good); }
  .pill.halt { background: #3a1818; color: var(--bad); }
  .pill.warn { background: #2e2511; color: var(--warn); }
  .pill.dim  { background: #1c2030; color: var(--dim); }
  .pos { color: var(--good); }
  .neg { color: var(--bad); }
  .neutral { color: var(--fg); }

  /* MC controls */
  .mc { display: flex; gap: 8px; align-items: center; }
  .mc button { background: transparent; color: var(--fg); border: 1px solid var(--border); border-radius: 6px; padding: 5px 10px; font-size: 11px; font-weight: 600; letter-spacing: 0.06em; cursor: pointer; font-family: inherit; }
  .mc button.active.freeze { background: #3a1818; border-color: var(--bad); color: var(--bad); }
  .mc button.active.half   { background: #2e2511; border-color: var(--warn); color: var(--warn); }
  .mc button.active.full   { background: #173023; border-color: var(--good); color: var(--good); }
  .mc button:hover:not(.active) { border-color: var(--accent); }

  /* Agent leaderboard */
  .agents { grid-template-columns: 1fr; gap: 10px; }
  .agent { display: grid; grid-template-columns: 110px 130px 1fr 110px 110px; gap: 14px; align-items: center; padding: 12px 16px; }
  .agent .who { display: flex; flex-direction: column; gap: 2px; }
  .agent .who .name { font-weight: 600; }
  .agent .who .model { font-size: 11px; color: var(--dim); font-family: ui-monospace, monospace; }
  .agent .equity { display: flex; flex-direction: column; gap: 2px; }
  .agent .equity .v { font-size: 18px; font-weight: 600; }
  .agent .equity .ret { font-size: 11px; }
  .agent .think { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .agent .think .latest { color: var(--fg); font-size: 13px; line-height: 1.45; overflow: hidden; }
  .agent .think .meta { font-size: 11px; color: var(--dim); display: flex; gap: 10px; }
  .agent .think .prev { font-size: 11px; color: var(--dim); display: flex; gap: 6px; }
  .agent .stat { display: flex; flex-direction: column; gap: 2px; }
  .agent .stat .v { font-size: 14px; font-weight: 600; }
  .agent .stat .l { font-size: 10px; color: var(--label); letter-spacing: 0.06em; text-transform: uppercase; }
  .dd-bar { height: 6px; background: #1c2030; border-radius: 3px; overflow: hidden; margin-top: 4px; }
  .dd-bar > div { height: 100%; }

  /* Charts row */
  .charts { grid-template-columns: 2fr 1fr; }
  .chart-card { display: flex; flex-direction: column; gap: 8px; min-height: 280px; }
  .chart-card h3 { margin: 0; font-size: 13px; font-weight: 600; }
  .chart-card .chart-wrap { position: relative; flex: 1; min-height: 240px; }

  /* Manager reserve card — distinct from sleeve agents */
  .agent-reserve { border-left: 3px solid var(--manager); background: linear-gradient(180deg, rgba(180,142,255,0.04) 0%, transparent 100%); }
  .agent-reserve .reserve-row { display: flex; align-items: baseline; justify-content: space-between; padding: 4px 0 6px; }
  .agent-reserve .reserve-label { color: var(--label); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
  .agent-reserve .reserve-v { font-size: 20px; font-weight: 600; color: var(--fg); }
  .agent-reserve .reserve-note { color: var(--dim); font-size: 11px; line-height: 1.4; padding: 4px 0 8px; border-bottom: 1px solid var(--border); margin-bottom: 6px; }

  /* Performance stripe on agent cards */
  .perf-stripe { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; padding: 6px 0; margin: 6px 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
  .perf-cell { display: flex; flex-direction: column; align-items: center; min-width: 0; }
  .perf-l { color: var(--label); font-size: 9px; letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 1px; }
  .perf-v { font-family: var(--mono); font-size: 12px; font-weight: 600; }
  .perf-good { color: var(--good); }
  .perf-mid { color: #d2b022; }
  .perf-bad { color: var(--bad); }
  .perf-na { color: var(--dim); }

  /* Outcome pills on activity rows */
  .outcome-pill { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 10px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; margin-right: 6px; vertical-align: middle; }
  .outcome-filled { background: #1f3a26; color: #6dd28a; }
  .outcome-rejected { background: #3a1f24; color: #f78d8d; }
  .outcome-vetoed { background: #3a2a1f; color: #f7b878; }
  .outcome-unsized { background: #2a2d36; color: #8891a3; }
  .outcome-cancelled, .outcome-expired { background: #2a2d36; color: #8891a3; }
  .outcome-pending { background: #1f2a3a; color: #79c0ff; }

  /* Position tabs */
  .pos-tabs { display: flex; gap: 4px; }
  .pos-tab { background: var(--panel-2); border: 1px solid var(--border); color: var(--dim); padding: 3px 9px; border-radius: 999px; font-size: 11px; cursor: pointer; user-select: none; font-family: inherit; }
  .pos-tab.active { background: var(--accent); color: #0b0d12; border-color: var(--accent); font-weight: 600; }

  /* Activity feed */
  .activity-controls { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
  .chip { background: var(--panel-2); border: 1px solid var(--border); color: var(--dim); padding: 4px 10px; border-radius: 999px; font-size: 11px; cursor: pointer; user-select: none; }
  .chip.on { background: var(--accent); color: #0b0d12; border-color: var(--accent); font-weight: 600; }
  .activity-search { background: var(--panel-2); border: 1px solid var(--border); color: var(--fg); border-radius: 6px; padding: 5px 10px; font-size: 12px; font-family: inherit; flex: 1; min-width: 140px; }
  table.feed { width: 100%; border-collapse: collapse; font-size: 12px; }
  table.feed th { text-align: left; color: var(--label); font-weight: 600; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  table.feed td { padding: 7px 8px; border-bottom: 1px solid #1a1e29; vertical-align: top; }
  table.feed tr:hover td { background: #161a23; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .dot.haiku { background: var(--haiku); }
  .dot.sonnet { background: var(--sonnet); }
  .dot.opus { background: var(--opus); }
  .dot.manager { background: var(--manager); }
  .dot.fill { background: var(--accent); }
  .action { font-weight: 600; }
  .action.buy, .action.long_open, .action.long_add { color: var(--buy); }
  .action.sell, .action.long_close, .action.long_trim { color: var(--sell); }
  .action.pass, .action.hold { color: var(--dim); }
  .rationale { color: var(--dim); max-width: 520px; }

  /* Positions + spend */
  .row3 { grid-template-columns: 1fr 1fr 1fr; }
  table.tight { width: 100%; border-collapse: collapse; font-size: 12px; }
  table.tight th { text-align: left; color: var(--label); font-weight: 600; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  table.tight td { padding: 7px 8px; border-bottom: 1px solid #1a1e29; }

  @media (max-width: 1100px) {
    .hero { grid-template-columns: 1fr 1fr; }
    .agent { grid-template-columns: 110px 110px 1fr; }
    .agent .stat { display: none; }
    .charts { grid-template-columns: 1fr; }
    .row3 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Multi-Agent Bot · Cockpit</h1>
    <span class="updated mono"><span id="updated">—</span></span>
  </header>

  <!-- Hero band -->
  <div class="grid hero" style="margin-bottom:14px;">
    <div class="card">
      <div class="label">NAV</div>
      <div class="big num" id="nav">—</div>
      <div class="sub"><span id="nav-status" class="pill dim">—</span> · regime: <span id="regime">—</span></div>
    </div>
    <div class="card">
      <div class="label">Day P&amp;L</div>
      <div class="big num" id="day-pnl">—</div>
      <div class="sub" id="day-pnl-pct">—</div>
    </div>
    <div class="card">
      <div class="label">vs SPY (today)</div>
      <div class="big num" id="alpha">—</div>
      <div class="sub" id="alpha-sub">today’s portfolio return minus SPY return</div>
    </div>
    <div class="card">
      <div class="label">LLM Spend Today</div>
      <div class="big num" id="spend">—</div>
      <div class="sub"><span id="spend-pct">—</span> of <span id="spend-limit" class="num">—</span> · forecast <span id="spend-eod" class="num">—</span></div>
    </div>
  </div>

  <!-- MC controls -->
  <div class="card mc" style="margin-bottom:14px; display:flex; align-items:center; gap:14px;">
    <span class="label">Master Capability</span>
    <button data-mc="0.0" class="freeze">FREEZE 0.0</button>
    <button data-mc="0.5" class="half">HALF 0.5</button>
    <button data-mc="1.0" class="full">FULL 1.0</button>
    <span style="margin-left:auto;" class="mono"><span class="label">current</span> <span id="mc-current">—</span>×</span>
    <span class="mono"><span class="label">eff. max gross</span> <span id="emg">—</span></span>
    <span class="mono"><span class="label">vix</span> <span id="vix">—</span></span>
  </div>

  <!-- Agent leaderboard -->
  <div class="card" style="margin-bottom:14px; padding:8px 0;">
    <div style="padding:6px 16px 8px;" class="label">Agent Leaderboard · ranked by sleeve equity</div>
    <div id="agents" class="grid agents"></div>
  </div>

  <!-- T1.5: per-sleeve P&L attribution snapshots (daily 16:45 ET) -->
  <div class="card" style="margin-bottom:14px;">
    <div style="padding:0 0 8px;" class="label">
      Per-Sleeve P&amp;L Attribution · last 10 days · realized + unrealized from lot ledger
    </div>
    <table class="tbl mono" style="width:100%;">
      <thead><tr>
        <th style="text-align:left;">Date</th>
        <th style="text-align:left;">Agent</th>
        <th style="text-align:right;">Realized</th>
        <th style="text-align:right;">Unrealized</th>
        <th style="text-align:right;">Total</th>
        <th style="text-align:right;">Open lots</th>
        <th style="text-align:right;">Closed lots</th>
      </tr></thead>
      <tbody id="agent-pnl-body"><tr><td colspan="7" class="dim">no data yet</td></tr></tbody>
    </table>
  </div>

  <!-- Charts row: NAV vs SPY + sleeve curves -->
  <div class="grid charts" style="margin-bottom:14px;">
    <div class="card chart-card">
      <h3>NAV vs SPY</h3>
      <div class="chart-wrap"><canvas id="nav-chart"></canvas></div>
    </div>
    <div class="card chart-card">
      <h3>Sleeve Equity</h3>
      <div class="chart-wrap"><canvas id="sleeve-chart"></canvas></div>
    </div>
  </div>

  <!-- Daily return comparison: bot vs SPY, one bar pair per trading day -->
  <div class="card chart-card" style="margin-bottom:14px;">
    <h3>Daily return: portfolio vs SPY</h3>
    <div class="chart-wrap" style="min-height:240px;"><canvas id="daily-vs-spy-chart"></canvas></div>
  </div>

  <!-- Activity feed -->
  <div class="card" style="margin-bottom:14px;">
    <div style="display:flex; align-items:center; gap:14px; margin-bottom:10px;">
      <div class="label">Activity</div>
      <div class="activity-controls" style="flex:1;">
        <span class="chip on" data-filter="all">All</span>
        <span class="chip" data-filter="fill">Fills</span>
        <span class="chip" data-filter="intent">Intents</span>
        <span class="chip" data-filter="haiku">Haiku</span>
        <span class="chip" data-filter="sonnet">Sonnet</span>
        <span class="chip" data-filter="opus">Opus</span>
        <span class="chip" data-filter="manager">Manager</span>
        <input id="search" class="activity-search" placeholder="search symbol or rationale…">
      </div>
    </div>
    <div style="max-height: 460px; overflow-y: auto;">
      <table class="feed">
        <thead><tr>
          <th>Time (ET)</th><th>Source</th><th>Symbol</th><th>Action</th>
          <th>Detail</th><th>Rationale</th>
        </tr></thead>
        <tbody id="activity"></tbody>
      </table>
    </div>
  </div>

  <!-- Bottom: positions, calibration, spend -->
  <div class="grid row3">
    <div class="card chart-card">
      <div style="display:flex; align-items:center; justify-content:space-between; gap:8px;">
        <h3 style="margin:0;">Open Positions</h3>
        <div id="pos-tabs" class="pos-tabs">
          <button class="pos-tab active" data-tab="all">All</button>
          <button class="pos-tab" data-tab="haiku">Haiku</button>
          <button class="pos-tab" data-tab="sonnet">Sonnet</button>
          <button class="pos-tab" data-tab="opus">Opus</button>
        </div>
      </div>
      <table class="tight">
        <thead><tr><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Mkt Val</th><th class="num">Unreal P&amp;L</th></tr></thead>
        <tbody id="positions"></tbody>
      </table>
    </div>
    <div class="card chart-card">
      <h3>Calibration · win rate by conviction</h3>
      <div class="chart-wrap"><canvas id="cal-chart"></canvas></div>
    </div>
    <div class="card chart-card">
      <h3>Cumulative Spend Today</h3>
      <div class="chart-wrap"><canvas id="spend-chart"></canvas></div>
    </div>
  </div>
</div>

<script>
const fmtUsd = (v, signed=false) => {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const sign = signed && v > 0 ? '+' : '';
  return sign + '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
};
const fmtPct = (v, signed=true) => {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const sign = signed && v > 0 ? '+' : '';
  return sign + (v * 100).toFixed(2) + '%';
};
const fmtPctRaw = (v) => v === null || v === undefined ? '—' : v.toFixed(1) + '%';
const classForPnl = (v) => v === null ? 'neutral' : (v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral');
const TZ = 'America/Chicago';
const fmtTimeET = (iso) => {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    const today = new Date();
    const sameDay =
      d.toLocaleDateString('en-US', { timeZone: TZ }) ===
      today.toLocaleDateString('en-US', { timeZone: TZ });
    const time = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: TZ, hour12: false });
    if (sameDay) return time;
    const date = d.toLocaleDateString('en-US', { month: 'numeric', day: 'numeric', timeZone: TZ });
    return `${date} ${time}`;
  } catch (e) { return iso; }
};
const fmtDate = (iso) => {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString('en-US', { month: 'numeric', day: 'numeric', timeZone: TZ });
  } catch (e) { return iso; }
};
const shortAgent = (full) => {
  if (!full) return '';
  const parts = String(full).split('.');
  return (parts[parts.length - 1] || full).toLowerCase();
};

// ─── Polling ───────────────────────────────────────────────────────────
let activityCache = [];
let activityFilters = { kind: 'all' };
let activitySearch = '';

async function fetchSnapshot() {
  try {
    const r = await fetch('/api/snapshot');
    const j = await r.json();
    renderTop(j.top);
    renderAgents(j.agents, j.positions_by_agent || {});
    renderPositions(j.positions, j.positions_by_agent || {});
    document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: TZ, hour12: false }) + ' CT';
  } catch (e) { console.error(e); }
}
async function fetchActivity() {
  try {
    const r = await fetch('/api/activity');
    activityCache = await r.json();
    renderActivity();
  } catch (e) { console.error(e); }
}
async function fetchNav() {
  try {
    const r = await fetch('/api/nav_curve');
    const j = await r.json();
    renderNav(j.nav, j.spy);
    computeAlpha(j.nav_today_return, j.spy_today_return);
  } catch (e) { console.error(e); }
}
async function fetchDailyVsSpy() {
  try {
    const r = await fetch('/api/daily_vs_spy');
    const j = await r.json();
    renderDailyVsSpy(j.daily || []);
  } catch (e) { console.error(e); }
}
async function fetchSleeves() {
  try {
    const r = await fetch('/api/sleeve_curves');
    const j = await r.json();
    renderSleeves(j);
  } catch (e) { console.error(e); }
}
async function fetchCal() {
  try {
    const r = await fetch('/api/calibration');
    const j = await r.json();
    renderCal(j);
  } catch (e) { console.error(e); }
}
async function fetchSpendCurve() {
  try {
    const r = await fetch('/api/spend_curve');
    const j = await r.json();
    renderSpendCurve(j);
  } catch (e) { console.error(e); }
}
async function fetchAgentPnl() {
  try {
    const r = await fetch('/api/agent_pnl?limit=10');
    const j = await r.json();
    renderAgentPnl(j);
  } catch (e) { console.error(e); }
}
function renderAgentPnl(rows) {
  const body = document.getElementById('agent-pnl-body');
  if (!body) return;
  if (!rows || rows.length === 0) {
    body.innerHTML = '<tr><td colspan="7" class="dim">no data yet</td></tr>';
    return;
  }
  const html = rows.map((r) => {
    const cls = (v) => classForPnl(v);
    return `<tr>
      <td>${r.date}</td>
      <td>${r.agent_id}</td>
      <td style="text-align:right;" class="${cls(r.realized)}">${fmtUsd(r.realized, true)}</td>
      <td style="text-align:right;" class="${cls(r.unrealized)}">${fmtUsd(r.unrealized, true)}</td>
      <td style="text-align:right;" class="${cls(r.total)}">${fmtUsd(r.total, true)}</td>
      <td style="text-align:right;">${r.num_open}</td>
      <td style="text-align:right;">${r.num_closed}</td>
    </tr>`;
  }).join('');
  body.innerHTML = html;
}

// ─── Renderers ─────────────────────────────────────────────────────────
function renderTop(t) {
  document.getElementById('nav').textContent = fmtUsd(t.total_nav);
  const dayPnl = t.day_pnl;
  const dayEl = document.getElementById('day-pnl');
  dayEl.textContent = fmtUsd(dayPnl, true);
  dayEl.className = 'big num ' + classForPnl(dayPnl);
  if (t.total_nav && dayPnl !== null) {
    const startNav = t.total_nav - dayPnl;
    const pct = startNav > 0 ? dayPnl / startNav : 0;
    document.getElementById('day-pnl-pct').textContent = fmtPct(pct, true);
  } else {
    document.getElementById('day-pnl-pct').textContent = '—';
  }
  const status = document.getElementById('nav-status');
  if (t.halted) { status.className = 'pill halt'; status.textContent = 'HALTED'; }
  else { status.className = 'pill live'; status.textContent = 'LIVE'; }
  document.getElementById('regime').textContent = t.regime || '—';
  document.getElementById('spend').textContent = fmtUsd(t.day_spend);
  document.getElementById('spend-pct').textContent = (t.spend_pct || 0).toFixed(0) + '%';
  document.getElementById('spend-limit').textContent = fmtUsd(t.spend_limit);
  document.getElementById('mc-current').textContent = (t.master_capability || 0).toFixed(2);
  document.getElementById('emg').textContent = (t.effective_max_gross || 0).toFixed(2);
  document.getElementById('vix').textContent = t.vix_bucket || '—';
  // Highlight the active MC button
  document.querySelectorAll('.mc button').forEach(b => {
    const v = parseFloat(b.dataset.mc);
    b.classList.toggle('active', Math.abs(v - t.master_capability) < 1e-6);
  });
}

function renderPerfStripe(perf, brier) {
  const fmtRatio = (v) => (v === null || v === undefined || isNaN(v)) ? '—' : v.toFixed(2);
  const fmtPctVal = (v) => (v === null || v === undefined || isNaN(v)) ? '—' : (v * 100).toFixed(0) + '%';
  const ratioClass = (v) => {
    if (v === null || v === undefined || isNaN(v)) return 'perf-na';
    if (v > 0.5) return 'perf-good';
    if (v > 0) return 'perf-mid';
    return 'perf-bad';
  };
  const sharpe = perf.sharpe_4w;
  const sortino = perf.sortino_4w;
  const dd = perf.max_dd_4w ?? 0;
  const wr = perf.win_rate;
  const lr = perf.loss_rate;
  const n = perf.n_closed || 0;
  return `
    <div class="perf-stripe">
      <div class="perf-cell"><span class="perf-l">Sharpe</span><span class="perf-v ${ratioClass(sharpe)}">${fmtRatio(sharpe)}</span></div>
      <div class="perf-cell"><span class="perf-l">Sortino</span><span class="perf-v ${ratioClass(sortino)}">${fmtRatio(sortino)}</span></div>
      <div class="perf-cell"><span class="perf-l">MaxDD</span><span class="perf-v" style="color:${dd < -0.05 ? 'var(--bad)' : 'var(--dim)'}">${(dd * 100).toFixed(1)}%</span></div>
      <div class="perf-cell"><span class="perf-l">Win</span><span class="perf-v">${fmtPctVal(wr)}</span></div>
      <div class="perf-cell"><span class="perf-l">Loss</span><span class="perf-v">${fmtPctVal(lr)}</span></div>
      <div class="perf-cell"><span class="perf-l">N</span><span class="perf-v">${n}</span></div>
      <div class="perf-cell"><span class="perf-l">Brier</span><span class="perf-v">${(brier || 0).toFixed(3)}</span></div>
    </div>`;
}

function renderAgents(agents, positionsByAgent = {}) {
  // Sort by sleeve equity desc (live snapshot first, then summary)
  const sorted = [...agents].sort((a, b) => {
    const ea = a.sleeve_equity_live ?? a.sleeve_equity ?? 0;
    const eb = b.sleeve_equity_live ?? b.sleeve_equity ?? 0;
    return eb - ea;
  });
  const root = document.getElementById('agents');
  root.innerHTML = '';
  sorted.forEach(a => {
    const short = shortAgent(a.agent_id);
    const equity = a.sleeve_equity_live ?? a.sleeve_equity;
    const ret4w = a.four_week_return_pct;
    const dd = a.drawdown_pct ?? 0;
    const ddBucket = a.drawdown_bucket || 'ok';
    const ddColor = ddBucket === 'halt' ? 'var(--bad)' : ddBucket === 'warning' ? 'var(--warn)' : ddBucket === 'halved' ? 'var(--warn)' : 'var(--good)';
    const ddWidth = Math.min(100, dd * 100 / 0.20).toFixed(0); // 20% drawdown = full bar

    if (short === 'manager') {
      const latestMgr = a.intents[0];
      const mgrMeta = latestMgr
        ? `<div class="meta"><span>${escapeHtml(latestMgr.action || '')}</span><span>${fmtTimeET(latestMgr.ts)} CT</span></div>`
        : `<div class="meta" style="color:var(--dim)">awaiting Friday call</div>`;
      root.innerHTML += `
        <div class="card agent agent-reserve">
          <div class="who">
            <span class="name"><span class="dot manager"></span>MANAGER</span>
            <span class="model">${a.model}</span>
          </div>
          <div class="reserve-row">
            <span class="reserve-label">Reserve</span>
            <span class="reserve-v num">${fmtUsd(equity)}</span>
          </div>
          <div class="reserve-note">CIO role — capital allocator, holds no positions. Runs Fridays 17:00 ET for sleeve reweighting.</div>
          <div class="think">${mgrMeta}</div>
        </div>`;
      return;
    }

    const perf = a.performance || {};
    const perfStripe = renderPerfStripe(perf, a.brier_score || 0);
    const posList = positionsByAgent[short] || [];
    const posValue = posList.reduce((s, p) => s + Math.abs(Number(p.market_value) || 0), 0);
    const exposure = (equity && equity > 0) ? (posValue / equity) : null;
    const exposureStr = exposure === null ? '—' : (exposure * 100).toFixed(1) + '%';
    root.innerHTML += `
      <div class="card agent">
        <div class="who">
          <span class="name"><span class="dot ${short}"></span>${short.toUpperCase()}</span>
          <span class="model">${a.model}</span>
        </div>
        <div class="equity">
          <span class="v num">${fmtUsd(equity)}</span>
          <span class="ret ${classForPnl(ret4w)} num">${ret4w === null ? '—' : fmtPct(ret4w/100, true)} 4w</span>
          <span class="ret num" title="positions value / sleeve equity" style="color:var(--dim)">${exposureStr} expo</span>
        </div>
        ${perfStripe}
        <div class="stat">
          <span class="l">Drawdown</span>
          <span class="v num" style="color:${ddColor}">${(dd * 100).toFixed(1)}%</span>
          <div class="dd-bar"><div style="width:${ddWidth}%;background:${ddColor};"></div></div>
        </div>
      </div>`;
  });
}

let positionsCache = { all: [], byAgent: {} };
let activePositionsTab = 'all';

function renderPositions(positions, byAgent) {
  if (positions !== undefined) positionsCache.all = positions || [];
  if (byAgent !== undefined) positionsCache.byAgent = byAgent || {};

  const root = document.getElementById('positions');
  const list = activePositionsTab === 'all'
    ? positionsCache.all
    : (positionsCache.byAgent[activePositionsTab] || []);

  if (!list.length) {
    const label = activePositionsTab === 'all' ? 'no open positions' : `no open positions for ${activePositionsTab}`;
    root.innerHTML = `<tr><td colspan="5" style="color:var(--dim); text-align:center; padding:18px;">${label}</td></tr>`;
    return;
  }
  root.innerHTML = list.map(p => `
    <tr>
      <td class="mono">${escapeHtml(p.symbol)}</td>
      <td><span class="pill ${p.side === 'long' ? 'live' : 'halt'}">${p.side}</span></td>
      <td class="num">${p.qty.toFixed(4)}</td>
      <td class="num">${fmtUsd(p.market_value)}</td>
      <td class="num ${classForPnl(p.unrealized_pl)}">${fmtUsd(p.unrealized_pl, true)}</td>
    </tr>`).join('');
}

document.addEventListener('DOMContentLoaded', () => {
  const tabs = document.getElementById('pos-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', (e) => {
    const btn = e.target.closest('.pos-tab');
    if (!btn) return;
    activePositionsTab = btn.dataset.tab;
    tabs.querySelectorAll('.pos-tab').forEach(b => b.classList.toggle('active', b === btn));
    renderPositions();
  });
});

function renderOutcomePill(r) {
  if (r.kind === 'fill') {
    return `<span class="outcome-pill outcome-filled">filled</span>`;
  }
  const outcome = r.outcome || '';
  if (!outcome) {
    return `<span class="outcome-pill outcome-pending">pending</span>`;
  }
  // outcome strings: "filled" | "vetoed:<reason>" | "rejected:<msg>" |
  // "unsized:sub_min" | "unsized:no_position" | "cancelled" | "expired" | …
  const head = String(outcome).split(':')[0];
  const tail = String(outcome).slice(head.length + 1);
  const cls = `outcome-${head}`;
  const label = tail ? `${head}: ${tail.slice(0, 40)}` : head;
  return `<span class="outcome-pill ${cls}" title="${escapeHtml(outcome)}">${escapeHtml(label)}</span>`;
}

function renderActivity() {
  const tbody = document.getElementById('activity');
  const filtered = activityCache.filter(r => {
    if (activityFilters.kind === 'all') return true;
    if (activityFilters.kind === 'fill') return r.kind === 'fill';
    if (activityFilters.kind === 'intent') return r.kind === 'intent';
    return r.kind === 'intent' && shortAgent(r.agent) === activityFilters.kind;
  }).filter(r => {
    if (!activitySearch) return true;
    const s = activitySearch.toLowerCase();
    return (r.symbol || '').toLowerCase().includes(s) || (r.rationale || '').toLowerCase().includes(s);
  });
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--dim); text-align:center; padding:18px;">no events</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(r => {
    const fillAgent = r.kind === 'fill' ? shortAgent(r.agent) : '';
    const agent = r.kind === 'intent' ? shortAgent(r.agent) : (fillAgent || 'fill');
    const detail = r.kind === 'fill'
      ? `<span class="num">${(r.qty || 0).toFixed(4)} @ ${fmtUsd(r.price)} = ${fmtUsd(r.total_cost)}</span>`
      : `<span class="mono">conviction ${r.conviction || 0}</span>`;
    const label = r.kind === 'fill'
      ? `FILL${fillAgent ? ' · ' + fillAgent.toUpperCase() : ''}`
      : agent.toUpperCase();
    const pill = renderOutcomePill(r);
    return `<tr>
      <td class="mono" style="color:var(--dim);">${fmtTimeET(r.ts)}</td>
      <td><span class="dot ${agent}"></span>${label}</td>
      <td class="mono">${escapeHtml(r.symbol)}</td>
      <td class="action ${r.action}">${(r.action || '').toUpperCase()}</td>
      <td>${detail}</td>
      <td class="rationale">${pill}${escapeHtml(r.rationale || '')}</td>
    </tr>`;
  }).join('');
}

document.querySelectorAll('.chip').forEach(c => {
  c.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(x => x.classList.remove('on'));
    c.classList.add('on');
    activityFilters.kind = c.dataset.filter;
    renderActivity();
  });
});
document.getElementById('search').addEventListener('input', (e) => {
  activitySearch = e.target.value;
  renderActivity();
});

document.querySelectorAll('.mc button').forEach(b => {
  b.addEventListener('click', async () => {
    const v = parseFloat(b.dataset.mc);
    await fetch('/api/master_capability', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: v }) });
    fetchSnapshot();
  });
});

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

// ─── Charts ────────────────────────────────────────────────────────────
const chartDefaults = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { labels: { color: '#e6e8ee', font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: '#8891a3', font: { size: 10 } }, grid: { color: '#1a1e29' } },
    y: { ticks: { color: '#8891a3', font: { size: 10 } }, grid: { color: '#1a1e29' } },
  },
};

let navChart, sleeveChart, calChart, spendChart, dailyVsSpyChart;

function renderNav(nav, spy) {
  const ctx = document.getElementById('nav-chart').getContext('2d');
  const data = {
    datasets: [
      { label: 'NAV', data: nav.map(p => ({ x: p.ts, y: p.nav })), borderColor: '#79c0ff', backgroundColor: '#79c0ff22', borderWidth: 2, pointRadius: 0, tension: 0.2, fill: false },
      { label: 'SPY (anchored)', data: spy.map(p => ({ x: p.ts, y: p.value })), borderColor: '#8891a3', borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: false },
    ],
  };
  if (navChart) { navChart.data = data; navChart.update('none'); return; }
  navChart = new Chart(ctx, {
    type: 'line', data,
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, tooltip: { callbacks: { title: (items) => fmtTimeET(items[0].parsed.x) } } }, scales: { ...chartDefaults.scales, x: { ...chartDefaults.scales.x, type: 'time', time: { unit: 'day', displayFormats: { day: 'M/d' } } } } },
  });
}

function computeAlpha(navRet, spyRet) {
  const el = document.getElementById('alpha');
  const sub = document.getElementById('alpha-sub');
  if (navRet == null || spyRet == null) {
    el.textContent = '—';
    if (sub) sub.textContent = 'today’s portfolio return minus SPY return';
    return;
  }
  const alpha = navRet - spyRet;
  el.textContent = (alpha > 0 ? '+' : '') + (alpha * 100).toFixed(2) + '%';
  el.className = 'big num ' + classForPnl(alpha);
  if (sub) {
    const fmt = (r) => (r > 0 ? '+' : '') + (r * 100).toFixed(2) + '%';
    sub.textContent = `port ${fmt(navRet)} · SPY ${fmt(spyRet)}`;
  }
}

function renderDailyVsSpy(rows) {
  const ctx = document.getElementById('daily-vs-spy-chart').getContext('2d');
  if (!rows || rows.length === 0) {
    if (dailyVsSpyChart) { dailyVsSpyChart.data = { labels: [], datasets: [] }; dailyVsSpyChart.update('none'); }
    return;
  }
  const labels = rows.map(r => r.date);
  const navPct  = rows.map(r => +(r.nav_pct  * 100).toFixed(2));
  const spyPct  = rows.map(r => +(r.spy_pct  * 100).toFixed(2));
  // Cumulative alpha line so you can see compounding outperformance over time.
  let cum = 0;
  const alphaCum = rows.map(r => { cum += r.alpha_pct * 100; return +cum.toFixed(2); });
  const data = {
    labels,
    datasets: [
      { type: 'bar', label: 'Portfolio %', data: navPct,
        backgroundColor: navPct.map(v => v >= 0 ? '#3fb95066' : '#f8514966'),
        borderColor:     navPct.map(v => v >= 0 ? '#3fb950'   : '#f85149'),
        borderWidth: 1, borderRadius: 2, categoryPercentage: 0.8, barPercentage: 0.9 },
      { type: 'bar', label: 'SPY %', data: spyPct,
        backgroundColor: '#8891a366', borderColor: '#8891a3',
        borderWidth: 1, borderRadius: 2, categoryPercentage: 0.8, barPercentage: 0.9 },
      { type: 'line', label: 'Cum. alpha (port − SPY)', data: alphaCum,
        borderColor: '#d2a8ff', backgroundColor: '#d2a8ff22',
        borderWidth: 2, pointRadius: 2, tension: 0.2, fill: false, yAxisID: 'y1' },
    ],
  };
  const opts = {
    ...chartDefaults,
    plugins: { ...chartDefaults.plugins,
      tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y >= 0 ? '+' : ''}${ctx.parsed.y.toFixed(2)}%` } } },
    scales: {
      x: { ...chartDefaults.scales.x, grid: { display: false } },
      y: { ...chartDefaults.scales.y, title: { display: true, text: 'daily %', color: '#8891a3' },
           ticks: { ...(chartDefaults.scales.y?.ticks || {}), callback: (v) => v + '%' } },
      y1: { position: 'right', grid: { display: false }, ticks: { color: '#d2a8ff', callback: (v) => v + '%' },
            title: { display: true, text: 'cum. alpha %', color: '#d2a8ff' } },
    },
  };
  if (dailyVsSpyChart) { dailyVsSpyChart.data = data; dailyVsSpyChart.options = opts; dailyVsSpyChart.update('none'); return; }
  dailyVsSpyChart = new Chart(ctx, { type: 'bar', data, options: opts });
}

function renderSleeves(byAgent) {
  const ctx = document.getElementById('sleeve-chart').getContext('2d');
  const colors = { haiku: '#79c0ff', sonnet: '#d2a8ff', opus: '#ffa657', manager: '#7ee787' };
  const datasets = Object.entries(byAgent).map(([agent, pts]) => ({
    label: agent, data: pts.map(p => ({ x: p.ts, y: p.equity })),
    borderColor: colors[agent] || '#e6e8ee', borderWidth: 1.6, pointRadius: 0, tension: 0.2, fill: false,
  }));
  const data = { datasets };
  if (sleeveChart) { sleeveChart.data = data; sleeveChart.update('none'); return; }
  sleeveChart = new Chart(ctx, {
    type: 'line', data,
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, tooltip: { callbacks: { title: (items) => fmtTimeET(items[0].parsed.x) } } }, scales: { ...chartDefaults.scales, x: { ...chartDefaults.scales.x, type: 'time', time: { unit: 'hour', displayFormats: { hour: 'HH:mm', day: 'M/d' } } } } },
  });
}

function renderCal(points) {
  const ctx = document.getElementById('cal-chart').getContext('2d');
  const colors = { haiku: '#79c0ff', sonnet: '#d2a8ff', opus: '#ffa657', manager: '#7ee787' };
  const byAgent = {};
  points.forEach(p => { (byAgent[p.agent] ||= []).push({ x: p.conviction, y: p.win_rate * 100, r: Math.max(4, Math.min(18, 2 + p.n)) }); });
  const datasets = Object.entries(byAgent).map(([agent, pts]) => ({
    label: agent, data: pts, backgroundColor: (colors[agent] || '#e6e8ee') + 'cc',
  }));
  // Add a perfect-calibration reference line via separate dataset
  datasets.push({
    type: 'line', label: 'perfect',
    data: [{ x: 1, y: 10 }, { x: 10, y: 100 }],
    borderColor: '#3a4254', borderDash: [3, 3], borderWidth: 1, pointRadius: 0, fill: false,
  });
  const data = { datasets };
  const opts = { ...chartDefaults,
    scales: {
      x: { ...chartDefaults.scales.x, title: { display: true, text: 'conviction', color: '#8891a3' }, min: 1, max: 10 },
      y: { ...chartDefaults.scales.y, title: { display: true, text: 'win rate %', color: '#8891a3' }, min: 0, max: 100 },
    } };
  if (calChart) { calChart.data = data; calChart.options = opts; calChart.update('none'); return; }
  calChart = new Chart(ctx, { type: 'bubble', data, options: opts });
}

function renderSpendCurve(points) {
  const ctx = document.getElementById('spend-chart').getContext('2d');
  const data = {
    datasets: [
      { label: 'cumulative', data: points.map(p => ({ x: p.ts, y: p.cumulative })), borderColor: '#d29922', backgroundColor: '#d2992233', borderWidth: 2, pointRadius: 0, tension: 0.2, fill: true },
    ],
  };
  if (spendChart) { spendChart.data = data; spendChart.update('none'); return; }
  spendChart = new Chart(ctx, {
    type: 'line', data,
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, tooltip: { callbacks: { title: (items) => fmtTimeET(items[0].parsed.x) } } }, scales: { ...chartDefaults.scales, x: { ...chartDefaults.scales.x, type: 'time', time: { unit: 'minute', displayFormats: { minute: 'HH:mm', hour: 'HH:mm' } } } } },
  });
}

// ─── Boot ──────────────────────────────────────────────────────────────
function refresh() {
  fetchSnapshot();
  fetchActivity();
  fetchNav();
  fetchDailyVsSpy();
  fetchSleeves();
  fetchCal();
  fetchSpendCurve();
  fetchAgentPnl();
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
