"""Dash component builders for the Bloomberg-lite terminal dashboard.

Pure functions: take DashboardData snapshots, return Dash components.
No I/O. Easy to unit-test by passing fake data.
"""

from __future__ import annotations

from typing import Any

from dash import html

from core.types import AgentId
from dashboard.data import (
    AgentSummary,
    DashboardData,
    FillRow,
    IntentRow,
    SpendBreakdown,
    TopStripMetrics,
)

# Dark "terminal" palette
_BG = "#0d1117"
_PANEL = "#161b22"
_BORDER = "#30363d"
_FG = "#c9d1d9"
_DIM = "#8b949e"
_ACCENT = "#58a6ff"
_OK = "#3fb950"
_WARN = "#d29922"
_ERR = "#f85149"


def render_top_strip(m: TopStripMetrics) -> html.Div:
    """One-line cockpit: NAV, spend, status, MC slider, regime, VIX bucket."""
    status_color = _ERR if m.halted else _OK
    status_text = "HALTED" if m.halted else "LIVE"
    spend_color = _ERR if m.spend_pct >= 90 else _WARN if m.spend_pct >= 70 else _OK

    cells = [
        _strip_cell("STATUS", status_text, color=status_color),
        _strip_cell("NAV", _fmt_money(m.total_nav)),
        _strip_cell("DAY P&L", _fmt_money(m.day_pnl_gross)),
        _strip_cell(
            "SPEND",
            f"${m.day_spend_usd:.4f} / ${m.spend_limit_usd:.2f} ({m.spend_pct:.0f}%)",
            color=spend_color,
        ),
        _strip_cell("MAX GROSS", f"{float(m.effective_max_gross):.2f}x"),
        _strip_cell("REGIME", m.regime_label.upper()),
        _strip_cell("VIX", m.vix_bucket.upper()),
        _strip_cell("HEARTBEAT", f"{m.heartbeat_age_s}s"),
        _strip_cell("APPR Q", str(m.approval_queue_count)),
    ]
    return html.Div(
        cells,
        style={
            "display": "flex",
            "gap": "0",
            "background": _PANEL,
            "border": f"1px solid {_BORDER}",
            "padding": "10px 14px",
            "fontFamily": "Menlo, monospace",
            "fontSize": "12px",
        },
    )


def render_agent_column(s: AgentSummary) -> html.Div:
    """Per-agent column: name/model, sleeve $, recent intents, calibration."""
    title = html.Div(
        [
            html.Span(s.agent_id.upper(), style={"color": _ACCENT, "fontWeight": "bold"}),
            html.Span(f"  ({s.model})", style={"color": _DIM, "fontSize": "11px"}),
        ]
    )
    sleeve = html.Div(
        [
            html.Span("Sleeve: ", style={"color": _DIM}),
            html.Span(_fmt_money(s.sleeve_equity)),
            html.Span("   4w: ", style={"color": _DIM, "marginLeft": "12px"}),
            html.Span(_fmt_pct(s.four_week_return_pct)),
        ],
        style={"marginTop": "6px", "fontSize": "12px"},
    )
    cal = html.Div(
        [
            html.Div("CALIBRATION", style={"color": _DIM, "fontSize": "10px", "marginTop": "10px"}),
            html.Div(f"Brier: {s.brier_score:.3f}"),
        ],
        style={"fontSize": "12px"},
    )
    intents = html.Div(
        [
            html.Div(
                "RECENT INTENTS",
                style={"color": _DIM, "fontSize": "10px", "marginTop": "10px"},
            ),
            *(_intent_row(i) for i in s.recent_intents[:5]),
        ]
    )
    return html.Div(
        [title, sleeve, cal, intents],
        style={
            "background": _PANEL,
            "border": f"1px solid {_BORDER}",
            "padding": "12px 14px",
            "flex": "1",
            "fontFamily": "Menlo, monospace",
            "fontSize": "12px",
            "color": _FG,
        },
    )


def render_intent_log(rows: list[IntentRow]) -> html.Div:
    """Bottom-strip intent log (most recent first)."""
    if not rows:
        return _empty_panel("INTENT LOG", "(no intents yet)")
    headers = ["time", "agent", "action", "symbol", "conv", "outcome", "rationale"]
    table = html.Table(
        [
            html.Thead(html.Tr([_th(h) for h in headers])),
            html.Tbody([
                html.Tr([
                    _td(r.timestamp[:19]),
                    _td(r.agent_id),
                    _td(r.action, color=_action_color(r.action)),
                    _td(r.symbol),
                    _td(str(r.conviction)),
                    _td(r.outcome or "—", color=_outcome_color(r.outcome)),
                    _td(r.rationale[:80]),
                ])
                for r in rows
            ]),
        ],
        style={"width": "100%", "borderCollapse": "collapse"},
    )
    return _wrap_panel("INTENT LOG", table)


def render_fill_log(rows: list[FillRow]) -> html.Div:
    if not rows:
        return _empty_panel("FILL LOG", "(no fills yet)")
    headers = ["time", "symbol", "side", "qty", "price"]
    table = html.Table(
        [
            html.Thead(html.Tr([_th(h) for h in headers])),
            html.Tbody([
                html.Tr([
                    _td(r.timestamp[:19]),
                    _td(r.symbol),
                    _td(r.side, color=_OK if r.side == "buy" else _WARN),
                    _td(f"{float(r.qty):.4f}"),
                    _td(f"${float(r.price):.2f}"),
                ])
                for r in rows
            ]),
        ],
        style={"width": "100%", "borderCollapse": "collapse"},
    )
    return _wrap_panel("FILL LOG", table)


def render_spend_panel(s: SpendBreakdown) -> html.Div:
    """Spend gauge: today $ vs $1.00 cap, by model + EOD forecast."""
    pct = float(s.today_total / s.daily_limit * 100) if s.daily_limit > 0 else 0
    color = _ERR if pct >= 90 else _WARN if pct >= 70 else _OK
    fc = float(s.eod_forecast)

    by_agent_lines = [
        html.Div(f"  {ag}: ${float(c):.4f}", style={"fontSize": "11px"})
        for ag, c in sorted(s.by_agent.items(), key=lambda kv: -kv[1])
    ] or [html.Div("  (no spend yet)", style={"color": _DIM, "fontSize": "11px"})]

    body = html.Div([
        html.Div(
            f"${float(s.today_total):.4f} / ${float(s.daily_limit):.2f}  ({pct:.1f}%)",
            style={"color": color, "fontSize": "16px", "fontWeight": "bold"},
        ),
        html.Div(f"EOD forecast: ${fc:.4f}", style={"color": _DIM, "fontSize": "11px"}),
        html.Div("BY AGENT:", style={"color": _DIM, "fontSize": "10px", "marginTop": "8px"}),
        *by_agent_lines,
    ])
    return _wrap_panel("SPEND", body)


def render_full_dashboard(data: DashboardData) -> html.Div:
    """Compose the entire page from a DashboardData snapshot."""
    top = render_top_strip(data.top_strip())
    agents = html.Div(
        [render_agent_column(data.agent_summary(a)) for a in (
            AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS, AgentId.MANAGER,
        )],
        style={"display": "flex", "gap": "8px", "marginTop": "8px"},
    )
    bottom_left = render_fill_log(data.recent_fills(50))
    bottom_right = render_intent_log(data.recent_intents(50))
    spend = render_spend_panel(data.spend_breakdown())

    bottom = html.Div(
        [
            html.Div(spend, style={"flex": "0 0 280px"}),
            html.Div(bottom_left, style={"flex": "1"}),
            html.Div(bottom_right, style={"flex": "2"}),
        ],
        style={"display": "flex", "gap": "8px", "marginTop": "8px"},
    )

    return html.Div(
        [top, agents, bottom],
        style={
            "background": _BG,
            "color": _FG,
            "minHeight": "100vh",
            "padding": "10px",
            "fontFamily": "Menlo, monospace",
        },
    )


# ─── Tiny helpers ─────────────────────────────────────────────────────────────


def _strip_cell(label: str, value: str, color: str = _FG) -> html.Div:
    return html.Div(
        [
            html.Div(label, style={"color": _DIM, "fontSize": "10px"}),
            html.Div(value, style={"color": color, "fontSize": "13px", "fontWeight": "bold"}),
        ],
        style={"padding": "0 16px", "borderRight": f"1px solid {_BORDER}"},
    )


def _intent_row(i: IntentRow) -> html.Div:
    badge_color = _action_color(i.action)
    return html.Div(
        [
            html.Span(i.action.upper(), style={"color": badge_color, "marginRight": "6px"}),
            html.Span(i.symbol, style={"fontWeight": "bold"}),
            html.Span(f"  c={i.conviction}", style={"color": _DIM, "marginLeft": "4px"}),
            html.Span(
                f"  → {i.outcome or '—'}",
                style={"color": _outcome_color(i.outcome), "marginLeft": "4px"},
            ),
        ],
        style={"fontSize": "11px", "padding": "2px 0", "borderBottom": f"1px solid {_BORDER}"},
    )


def _wrap_panel(title: str, body: Any) -> html.Div:
    return html.Div(
        [
            html.Div(title, style={"color": _DIM, "fontSize": "10px", "marginBottom": "6px"}),
            body,
        ],
        style={
            "background": _PANEL,
            "border": f"1px solid {_BORDER}",
            "padding": "10px 12px",
        },
    )


def _empty_panel(title: str, msg: str) -> html.Div:
    return _wrap_panel(title, html.Div(msg, style={"color": _DIM, "fontSize": "11px"}))


def _th(label: str) -> html.Th:
    return html.Th(
        label,
        style={
            "color": _DIM,
            "fontWeight": "normal",
            "textAlign": "left",
            "padding": "4px 8px",
            "borderBottom": f"1px solid {_BORDER}",
            "fontSize": "10px",
        },
    )


def _td(text: str, color: str = _FG) -> html.Td:
    return html.Td(
        text,
        style={
            "color": color,
            "padding": "3px 8px",
            "borderBottom": f"1px solid {_BORDER}",
            "fontSize": "11px",
        },
    )


def _action_color(action: str) -> str:
    a = action.lower()
    if a in ("buy", "add"):
        return _OK
    if a in ("sell", "trim", "exit", "close"):
        return _WARN
    return _FG


def _outcome_color(outcome: str | None) -> str:
    if outcome is None:
        return _DIM
    o = outcome.lower()
    if o == "win":
        return _OK
    if o == "loss":
        return _ERR
    return _DIM


def _fmt_money(v: Any) -> str:
    if v is None:
        return "—"
    return f"${float(v):,.2f}"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:+.2f}%"
