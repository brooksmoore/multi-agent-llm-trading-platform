"""Compute the Manager-agent's portfolio analytics from existing data sources.

The Manager's prompt references a dozen analytics that don't fit on the
hot path of agent dispatch — NAV history, sleeve Sortino, sector exposures,
tax events, etc. This module pulls them on-demand from the equity snapshot
DB, lot ledger, agent memories, and news store.

Each builder returns a formatted text block ready to drop into a
ManagerAgent user-message. We deliberately avoid passing structured data
to the LLM here — text blocks are easier to debug, cheaper to cache, and
the Manager's call types all expect prose context.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import sqrt
from pathlib import Path

from agents.memory import AgentMemory
from core.types import AgentId, NewsSource
from data.news_store import NewsStore
from execution.agent_state_tracker import AgentStateTracker
from execution.broker import Broker
from execution.lots import LotLedger


# Coarse sector classification covering DEFAULT_UNIVERSE. Anything not in
# this map falls into "Other" — that's still useful at the aggregate level.
_SECTOR_MAP: dict[str, str] = {
    # Broad-market ETFs
    "SPY": "Equity (US Broad)", "QQQ": "Equity (US Broad)", "IWM": "Equity (US Broad)",
    "EFA": "Equity (Intl DM)",  "EEM": "Equity (Intl EM)",
    # Bonds
    "TLT": "Bonds (Long Duration)", "IEF": "Bonds (Intermediate)",
    # Real assets
    "GLD": "Real Assets (Gold)", "USO": "Real Assets (Oil)", "VNQ": "Real Estate",
    # Levered tactical
    "TQQQ": "Equity (Levered)",
    # Single-name tech
    "AAPL": "Tech",  "NVDA": "Tech",  "MSFT": "Tech",
    "GOOGL": "Communication", "META": "Communication",
    "AMZN": "Consumer Discretionary",
    # Crypto
    "BTCUSD": "Crypto", "ETHUSD": "Crypto", "SOLUSD": "Crypto",
}

# Rough beta estimates for portfolio-beta calc. ETFs are exact-ish; single
# names use a 5-yr proxy. Beta = 0 for cash, 1.0 for SPY by definition.
_BETA_MAP: dict[str, Decimal] = {
    "SPY": Decimal("1.00"), "QQQ": Decimal("1.15"), "IWM": Decimal("1.10"),
    "EFA": Decimal("0.85"), "EEM": Decimal("0.90"),
    "TLT": Decimal("-0.20"), "IEF": Decimal("-0.10"),
    "GLD": Decimal("0.10"),  "USO": Decimal("0.40"), "VNQ": Decimal("0.85"),
    "TQQQ": Decimal("3.30"),
    "AAPL": Decimal("1.25"), "NVDA": Decimal("1.65"), "MSFT": Decimal("1.05"),
    "GOOGL": Decimal("1.10"), "META": Decimal("1.30"), "AMZN": Decimal("1.25"),
    "BTCUSD": Decimal("1.50"), "ETHUSD": Decimal("1.80"), "SOLUSD": Decimal("2.20"),
}


@dataclass
class ManagerContext:
    """Pre-formatted text blocks ready to splice into Manager user messages."""

    aggregate_nav: str = ""
    peak_nav: str = ""
    current_dd_pct: str = ""
    four_week_snapshot: str = ""
    macro_snapshot: str = ""
    portfolio_beta: str = ""
    sector_exposures: str = ""
    tax_events_summary: str = ""
    top_intents_this_week: str = ""
    prior_regime_read: str = ""
    calibration_summary_all: str = ""
    # Explicit feed-health summary placed at the top of every prompt block.
    # Each entry is "FEED_OK" or "FEED_ERROR:<reason>" — prevents the LLM from
    # misinterpreting sparse-but-valid data (e.g. cold-start 0% returns, no
    # closed lots) as a data pipeline outage.
    data_health: str = ""

    def as_prompt_block(self) -> str:
        """One-shot block suitable for prepending to any Manager user message."""
        sections = [
            ("Data feed health",       self.data_health),
            ("Aggregate NAV",          self.aggregate_nav),
            ("Peak NAV",               self.peak_nav),
            ("Current drawdown",       self.current_dd_pct),
            ("4-week sleeve snapshot", self.four_week_snapshot),
            ("Macro snapshot",         self.macro_snapshot),
            ("Portfolio beta",         self.portfolio_beta),
            ("Sector exposures",       self.sector_exposures),
            ("Tax events (30d)",       self.tax_events_summary),
            ("Top intents this week",  self.top_intents_this_week),
            ("Prior regime read",      self.prior_regime_read),
            ("Calibration summary",    self.calibration_summary_all),
        ]
        lines: list[str] = ["=== Manager portfolio context ==="]
        for label, body in sections:
            if not body:
                continue
            lines.append(f"\n--- {label} ---\n{body}")
        return "\n".join(lines)


def _q(v: Decimal | None, fmt: str = "{:,.2f}") -> str:
    return fmt.format(float(v)) if v is not None else "n/a"


# ── Equity history queries ────────────────────────────────────────────────────


def _query_equity_history(
    db_path: Path, since: datetime
) -> list[dict[str, Decimal | None]]:
    """Return equity_snapshots rows since `since`, oldest first."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT ts, total_nav, haiku_equity, sonnet_equity, opus_equity, "
            "       haiku_peak, sonnet_peak, opus_peak "
            "FROM equity_snapshots WHERE ts >= ? ORDER BY ts ASC",
            (since.isoformat(),),
        ).fetchall()
    finally:
        con.close()

    def _dec(x: str | None) -> Decimal | None:
        if x is None:
            return None
        try:
            return Decimal(x)
        except Exception:
            return None

    return [
        {
            "ts": row[0],
            "total_nav":     _dec(row[1]),
            "haiku_equity":  _dec(row[2]),
            "sonnet_equity": _dec(row[3]),
            "opus_equity":   _dec(row[4]),
            "haiku_peak":    _dec(row[5]),
            "sonnet_peak":   _dec(row[6]),
            "opus_peak":     _dec(row[7]),
        }
        for row in rows
    ]


def _daily_returns(equity_series: list[Decimal | None]) -> list[float]:
    """Compute simple daily returns from an equity series, skipping Nones."""
    series = [float(v) for v in equity_series if v is not None and v > 0]
    if len(series) < 2:
        return []
    return [
        (series[i] - series[i - 1]) / series[i - 1]
        for i in range(1, len(series))
    ]


def _sortino(returns: list[float], target: float = 0.0) -> float | None:
    """Annualized Sortino assuming roughly daily samples. None if insufficient data."""
    if len(returns) < 5:
        return None
    excess = [r - target for r in returns]
    downside = [r for r in excess if r < 0]
    if not downside:
        return None
    mean_excess = sum(excess) / len(excess)
    downside_var = sum(r * r for r in downside) / len(downside)
    downside_dev = sqrt(downside_var)
    if downside_dev == 0:
        return None
    return (mean_excess / downside_dev) * sqrt(252)


def _max_drawdown(equity_series: list[Decimal | None]) -> float:
    """Max drawdown over the series as a negative fraction (-0.07 = -7%)."""
    series = [float(v) for v in equity_series if v is not None and v > 0]
    if len(series) < 2:
        return 0.0
    peak = series[0]
    max_dd = 0.0
    for v in series:
        peak = max(peak, v)
        dd = (v - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


# ── Builders for each text block ──────────────────────────────────────────────


def _build_nav_blocks(
    history: list[dict[str, Decimal | None]],
    broker: Broker | None,
) -> tuple[str, str, str]:
    """Return (aggregate_nav, peak_nav, current_dd_pct) text blocks."""
    current_nav: Decimal | None = None
    if broker is not None:
        try:
            current_nav = broker.get_account().equity
        except Exception:
            current_nav = None

    nav_series = [r["total_nav"] for r in history if r["total_nav"] is not None]
    peak = max(nav_series) if nav_series else current_nav

    if current_nav is not None and peak is not None and peak > 0:
        dd = (current_nav - peak) / peak * Decimal("100")
        dd_pct = f"{float(dd):+.2f}% from peak"
    elif current_nav is not None:
        # Peak not yet established (cold-start); no drawdown has occurred.
        dd_pct = "0.00% from peak (cold-start — peak not yet established)"
    else:
        dd_pct = "n/a (broker unavailable)"

    history_days = len({r["ts"][:10] for r in history}) if history else 0
    peak_note = (
        f" ({history_days}d of history)"
        if history_days > 0
        else " (cold-start — no history yet)"
    )
    return (
        f"${_q(current_nav)}",
        f"${_q(peak)}{peak_note}",
        dd_pct,
    )


def _build_four_week_snapshot(
    history: list[dict[str, Decimal | None]],
    tracker: AgentStateTracker,
) -> str:
    """Per-sleeve 4-week table: start NAV, end NAV, return%, Sortino, max DD."""
    history_days = len({r["ts"][:10] for r in history}) if history else 0

    if not history:
        # Cold-start: no snapshot rows yet — report live tracker state and be
        # explicit so the LLM cannot mistake this for a data-pipeline outage.
        rows = [
            "COLD_START — equity_snapshots DB has no rows yet.",
            "Tracker state (live, not historical):",
        ]
        for agent in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
            try:
                s = tracker.get_state(agent)
                rows.append(
                    f"  {agent.value:7}: equity=${float(s.sleeve_equity):,.0f}  "
                    f"peak=${float(s.sleeve_peak_equity):,.0f}  "
                    f"bucket={s.drawdown_bucket.value}"
                )
            except Exception:
                rows.append(f"  {agent.value:7}: tracker unavailable")
        rows.append("Statistical metrics (return%, Sortino, maxDD) require ≥2 daily rows — not yet available.")
        return "\n".join(rows)

    lines = [
        f"  History: {history_days} calendar days of snapshots",
        f"  {'sleeve':7} | {'start':>10} | {'end':>10} | {'return':>8} | {'sortino':>8} | {'max_dd':>7}",
        f"  {'-'*7} | {'-'*10} | {'-'*10} | {'-'*8} | {'-'*8} | {'-'*7}",
    ]
    for agent_key, label in (
        ("haiku_equity", "haiku"),
        ("sonnet_equity", "sonnet"),
        ("opus_equity", "opus"),
    ):
        series: list[Decimal | None] = [r[agent_key] for r in history]  # type: ignore[misc]
        clean = [v for v in series if v is not None and v > 0]
        if len(clean) < 2:
            # Data feed is healthy — insufficient history, not a missing feed.
            note = f"FEED_OK / only {len(clean)} data point(s) — metrics need ≥2"
            lines.append(f"  {label:7} | {note}")
            continue
        start = clean[0]
        end = clean[-1]
        ret_pct = (float(end) - float(start)) / float(start) * 100
        rets = _daily_returns(series)
        sortino = _sortino(rets)
        sortino_str = f"{sortino:>+7.2f}" if sortino is not None else "  <5pts"
        max_dd = _max_drawdown(series)
        lines.append(
            f"  {label:7} | ${_q(start, '{:>9,.0f}')} | ${_q(end, '{:>9,.0f}')} | "
            f"{ret_pct:>+7.2f}% | {sortino_str:>8} | "
            f"{max_dd*100:>+6.2f}%"
        )
    return "\n".join(lines)


def _build_macro_snapshot(state_vix: Decimal | None, news_store: NewsStore) -> str:
    """VIX read + recent macro headlines (RSS-tagged Fed + market feeds)."""
    vix_str = (
        f"VIX: {float(state_vix):.2f}" if state_vix is not None else "VIX: n/a"
    )
    # Pull macro RSS items from the last 48h. Symbol filter is "" for RSS items.
    since = datetime.now(UTC) - timedelta(hours=48)
    # We don't have a cheap "all RSS" query; query by macro proxies instead.
    items = news_store.recent_for_symbols(
        ["SPY", "TLT", "GLD"], since=since, limit=80
    )
    rss_items = [n for n in items if n.source == NewsSource.RSS][:8]
    if not rss_items:
        return f"{vix_str}\n(no macro RSS items in last 48h)"
    blocks: list[str] = []
    for n in rss_items:
        when = f"{n.published_at:%m-%d %H:%M}"
        head = n.headline.strip()[:140]
        blocks.append(f"  - [{when}] {head}")
        summary = (n.summary or "").strip().replace("\n", " ")
        if summary:
            blocks.append(f"      {summary[:280]}")
    return f"{vix_str}\nRecent macro headlines:\n" + "\n".join(blocks)


def _build_portfolio_beta(broker: Broker | None) -> str:
    """Weighted-average beta of current holdings."""
    if broker is None:
        return "n/a (no broker)"
    try:
        positions = list(broker.list_positions())
        account = broker.get_account()
    except Exception:
        return "n/a (broker unavailable)"
    if not positions:
        return "0.00 (flat — all cash)"
    total_mv = sum(
        (p.qty * p.current_price for p in positions), Decimal("0")
    )
    equity = account.equity if account.equity > 0 else total_mv
    if equity <= 0:
        return "n/a (zero equity)"
    weighted_beta = Decimal("0")
    unmapped: list[str] = []
    for p in positions:
        beta = _BETA_MAP.get(p.symbol)
        if beta is None:
            unmapped.append(p.symbol)
            beta = Decimal("1.0")
        mv = p.qty * p.current_price
        weight = mv / equity
        weighted_beta += weight * beta
    note = f" (unmapped→β=1.0: {', '.join(unmapped)})" if unmapped else ""
    return f"{float(weighted_beta):+.2f}{note}"


def _build_sector_exposures(broker: Broker | None) -> str:
    """Dollar exposure by sector with portfolio-weight column."""
    if broker is None:
        return "n/a (no broker)"
    try:
        positions = list(broker.list_positions())
        account = broker.get_account()
    except Exception:
        return "n/a (broker unavailable)"
    if not positions:
        return "(flat — 100% cash)"
    equity = account.equity if account.equity > 0 else Decimal("1")
    by_sector: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for p in positions:
        mv = p.qty * p.current_price
        by_sector[_SECTOR_MAP.get(p.symbol, "Other")] += mv
    rows = sorted(by_sector.items(), key=lambda kv: kv[1], reverse=True)
    cash = account.cash
    return "\n".join(
        [f"  {sector:30} ${float(mv):>12,.0f}  ({float(mv/equity)*100:>+5.1f}%)"
         for sector, mv in rows]
        + [f"  {'Cash':30} ${float(cash):>12,.0f}  ({float(cash/equity)*100:>+5.1f}%)"]
    )


def _build_tax_events(lots: LotLedger, lookback_days: int = 30) -> str:
    """Closed lots in the last N days with realized P&L, split short/long-term."""
    cutoff: date = (datetime.now(UTC) - timedelta(days=lookback_days)).date()
    try:
        all_lots = lots.all_lots()
    except Exception as exc:
        return f"FEED_ERROR: lot ledger unavailable ({exc})"
    open_count = sum(1 for l in all_lots if not l.is_closed)
    closed = [
        l for l in all_lots
        if l.is_closed and l.exit_date is not None and l.exit_date >= cutoff
    ]
    if not closed:
        # Distinguish between "nothing closed" (healthy) and a broken feed.
        return (
            f"FEED_OK / 0 closed lots in last {lookback_days} days "
            f"(feed healthy — {open_count} open lot(s) tracked). "
            "No wash-sale, harvesting, or long-term-crossover events to act on."
        )
    short_term_pnl = Decimal("0")
    long_term_pnl = Decimal("0")
    short_count = 0
    long_count = 0
    for l in closed:
        pnl = l.realized_pnl or Decimal("0")
        if l.is_long_term:
            long_term_pnl += pnl
            long_count += 1
        else:
            short_term_pnl += pnl
            short_count += 1
    return (
        f"Last {lookback_days} days: {len(closed)} closed lots\n"
        f"  Short-term: {short_count} lots, realized P&L ${float(short_term_pnl):+,.2f}\n"
        f"  Long-term : {long_count} lots, realized P&L ${float(long_term_pnl):+,.2f}\n"
        f"  Total realized: ${float(short_term_pnl + long_term_pnl):+,.2f}"
    )


def _build_top_intents_week(
    memories: dict[AgentId, AgentMemory], limit_per_agent: int = 5
) -> str:
    """Cross-agent intent log over the last 7 days, ranked by conviction."""
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    rows: list[tuple[str, dict[str, str | int | None]]] = []
    feed_errors: list[str] = []
    for agent_id, mem in memories.items():
        if agent_id == AgentId.MANAGER:
            continue
        try:
            recent = mem.recent_intents_rows(n=20)
        except Exception as exc:
            feed_errors.append(f"{agent_id.value}: {exc}")
            continue
        for r in recent:
            logged = r.get("logged_at")
            if logged is None or str(logged) < cutoff:
                continue
            rows.append((agent_id.value, r))

    if feed_errors:
        return "FEED_ERROR: " + "; ".join(feed_errors)
    if not rows:
        # Feed is healthy — agents simply haven't issued intents this week yet.
        return "FEED_OK / 0 intents in last 7 days (memories readable, no intents logged yet)"
    rows.sort(key=lambda x: int(x[1].get("conviction") or 0), reverse=True)
    lines = [f"FEED_OK / {len(rows)} intent(s) in last 7 days:"]
    for agent, r in rows[:limit_per_agent * 3]:
        outcome = r.get("outcome") or "—"
        rationale = (str(r.get("rationale") or ""))[:90]
        lines.append(
            f"  [{agent:6}] {r.get('action'):>12} {r.get('symbol'):8} "
            f"conv={r.get('conviction')} → {outcome}: {rationale}"
        )
    return "\n".join(lines)


def _build_prior_regime_read(memory: AgentMemory) -> str:
    prior = memory.recall("last_regime_read")
    if not prior:
        return "(no prior regime read recorded)"
    # Trim if a long JSON blob was stashed.
    return prior[:1200]


def _build_calibration_summary(
    memories: dict[AgentId, AgentMemory], lookback_days: int = 30
) -> str:
    """Per-agent intent count + outcome distribution over last N days."""
    cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).isoformat()
    lines = []
    for agent_id in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
        mem = memories.get(agent_id)
        if mem is None:
            continue
        try:
            rows = mem.recent_intents_rows(n=100)
        except Exception:
            continue
        recent = [r for r in rows if str(r.get("logged_at") or "") >= cutoff]
        if not recent:
            lines.append(f"  {agent_id.value:7}: 0 intents in last {lookback_days}d")
            continue
        outcomes: dict[str, int] = defaultdict(int)
        conv_sum = 0
        for r in recent:
            outcomes[str(r.get("outcome") or "open")] += 1
            conv_sum += int(r.get("conviction") or 0)
        avg_conv = conv_sum / len(recent)
        outcome_str = ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
        lines.append(
            f"  {agent_id.value:7}: {len(recent)} intents, avg_conv={avg_conv:.1f}, "
            f"outcomes: {outcome_str}"
        )
    return "\n".join(lines) if lines else "(no calibration data yet)"


def _build_data_health(
    history: list[dict[str, Decimal | None]],
    snapshot_db: Path,
    memories: dict[AgentId, AgentMemory],
    broker: Broker | None,
    lots: LotLedger,
    since: datetime,
) -> str:
    """Explicit feed-health summary. ALWAYS rendered first in the prompt block.

    Purpose: prevent the LLM from misinterpreting sparse-but-valid data
    (e.g. cold-start 0% returns, no closed lots, zero intents on day 1) as a
    data-pipeline outage.  Each line is "FEED_OK" or "FEED_ERROR:<reason>".
    """
    lines: list[str] = []
    history_days = len({r["ts"][:10] for r in history})

    # 1. Equity snapshot DB
    if not snapshot_db.exists():
        lines.append("equity_snapshots : FEED_ERROR: DB file missing")
    elif not history:
        lines.append(
            f"equity_snapshots : FEED_OK / 0 rows since {since.date()} "
            f"(cold-start — DB exists, no rows yet)"
        )
    else:
        lines.append(
            f"equity_snapshots : FEED_OK / {len(history)} rows "
            f"over {history_days} calendar day(s)"
        )

    # 2. Broker / NAV
    if broker is None:
        lines.append("broker           : FEED_ERROR: no broker configured (paper/sim only)")
    else:
        try:
            acct = broker.get_account()
            lines.append(f"broker           : FEED_OK / equity=${float(acct.equity):,.2f}")
        except Exception as exc:
            lines.append(f"broker           : FEED_ERROR: {exc}")

    # 3. Lot ledger
    try:
        open_lots = sum(1 for l in lots.all_lots() if not l.is_closed)
        lines.append(f"lot_ledger       : FEED_OK / {open_lots} open lot(s)")
    except Exception as exc:
        lines.append(f"lot_ledger       : FEED_ERROR: {exc}")

    # 4. Agent memories / intent logs
    for agent_id in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
        mem = memories.get(agent_id)
        if mem is None:
            lines.append(f"memory.{agent_id.value:6} : FEED_ERROR: memory object missing")
            continue
        try:
            rows = mem.recent_intents_rows(n=5)
            lines.append(f"memory.{agent_id.value:6} : FEED_OK / {len(rows)} recent intent row(s) readable")
        except Exception as exc:
            lines.append(f"memory.{agent_id.value:6} : FEED_ERROR: {exc}")

    return "\n".join(lines)


# ── Public entry point ────────────────────────────────────────────────────────


def build_manager_context(
    *,
    state_vix: Decimal | None,
    snapshot_db: Path,
    tracker: AgentStateTracker,
    lots: LotLedger,
    broker: Broker | None,
    news_store: NewsStore,
    memories: dict[AgentId, AgentMemory],
    lookback_days: int = 28,
) -> ManagerContext:
    """Assemble the full Manager context. Each section degrades gracefully on error."""
    since = datetime.now(UTC) - timedelta(days=lookback_days)
    history = _query_equity_history(snapshot_db, since)

    aggregate_nav, peak_nav, dd_pct = _build_nav_blocks(history, broker)
    return ManagerContext(
        data_health=_build_data_health(history, snapshot_db, memories, broker, lots, since),
        aggregate_nav=aggregate_nav,
        peak_nav=peak_nav,
        current_dd_pct=dd_pct,
        four_week_snapshot=_build_four_week_snapshot(history, tracker),
        macro_snapshot=_build_macro_snapshot(state_vix, news_store),
        portfolio_beta=_build_portfolio_beta(broker),
        sector_exposures=_build_sector_exposures(broker),
        tax_events_summary=_build_tax_events(lots),
        top_intents_this_week=_build_top_intents_week(memories),
        prior_regime_read=_build_prior_regime_read(memories[AgentId.MANAGER]),
        calibration_summary_all=_build_calibration_summary(memories),
    )
