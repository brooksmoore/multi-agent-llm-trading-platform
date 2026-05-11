"""Read-only data adapter for the dashboard.

Aggregates from OMSStore, AgentMemory(s), CalibrationTracker, and BudgetLedger.
Never mutates state. Per blueprint Principle: "the dashboard is read-only.
It polls SQLite/DuckDB every 3s; it never mutates state and is not on the
trading code path."
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from agents.calibration import CalibrationTracker
from agents.memory import AgentMemory
from core.types import AgentId
from execution.budget import BudgetLedger
from execution.oms_store import EventKind, OMSStore

# ─── Dataclasses for each panel ───────────────────────────────────────────────


@dataclass(frozen=True)
class LiveMetrics:
    """Snapshot of runtime values that the dashboard's top strip displays.

    Produced by App.live_metrics() each tick so the dashboard never lies about
    master_capability, effective_max_gross, or vix_bucket.
    """

    master_capability: Decimal
    effective_max_gross: Decimal
    vix_bucket: str
    halted: bool


@dataclass(frozen=True)
class TopStripMetrics:
    total_nav: Decimal | None
    day_pnl_gross: Decimal | None
    day_spend_usd: Decimal
    spend_limit_usd: Decimal
    spend_pct: float
    halted: bool
    master_capability: Decimal
    effective_max_gross: Decimal
    regime_label: str
    vix_bucket: str
    heartbeat_age_s: int
    approval_queue_count: int


@dataclass(frozen=True)
class IntentRow:
    timestamp: str
    agent_id: str
    symbol: str
    action: str
    conviction: int
    outcome: str | None
    rationale: str


@dataclass(frozen=True)
class FillRow:
    timestamp: str
    order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    total_cost: Decimal
    agent_id: str
    rationale: str


@dataclass(frozen=True)
class SpendBreakdown:
    today_total: Decimal
    daily_limit: Decimal
    by_call_type: dict[str, Decimal]
    by_agent: dict[str, Decimal]
    eod_forecast: Decimal


@dataclass(frozen=True)
class SleeveCurvePoint:
    ts: str
    agent_id: str
    equity: Decimal


@dataclass(frozen=True)
class NavPoint:
    ts: str
    total_nav: Decimal | None


@dataclass(frozen=True)
class SpendPoint:
    ts: str
    cumulative_usd: Decimal


@dataclass(frozen=True)
class CalibrationPoint:
    agent_id: str
    conviction_bucket: int
    win_rate: float
    n: int


@dataclass(frozen=True)
class DrawdownPoint:
    agent_id: str
    drawdown_pct: float
    bucket: str


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    qty: Decimal
    market_value: Decimal
    side: str
    unrealized_pl: Decimal | None


@dataclass(frozen=True)
class AgentSummary:
    agent_id: str
    model: str
    sleeve_equity: Decimal | None
    four_week_return_pct: Decimal | None
    recent_intents: list[IntentRow] = field(default_factory=list)
    brier_score: float = 0.0
    calibration_table: list[dict[str, Any]] = field(default_factory=list)
    performance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentPerformance:
    """4-week annualized risk-adjusted return + win/loss stats per agent.

    sharpe / sortino are None when fewer than 5 daily returns are available.
    win_rate / loss_rate are None when no closed trades exist yet.
    """
    sharpe_4w: float | None
    sortino_4w: float | None
    max_dd_4w: float
    win_rate: float | None
    loss_rate: float | None
    n_closed: int


# ─── Adapter ──────────────────────────────────────────────────────────────────


_AGENT_MODEL: dict[AgentId, str] = {
    AgentId.HAIKU: "claude-haiku-4-5",
    AgentId.SONNET: "claude-sonnet-4-6",
    AgentId.OPUS: "claude-opus-4-7",
    AgentId.MANAGER: "claude-opus-4-7",
}


class DashboardData:
    """Read-only aggregate of all dashboard data sources.

    Every method is a pure read. Callers may invoke any method at any time
    (e.g. on a 3s Dash interval); the underlying stores manage their own locks.
    """

    def __init__(
        self,
        oms_store: OMSStore | None = None,
        memories: dict[AgentId, AgentMemory] | None = None,
        calibration: CalibrationTracker | None = None,
        budget: BudgetLedger | None = None,
        master_capability: Decimal = Decimal("1.0"),
        effective_max_gross: Decimal = Decimal("1.25"),
        regime_label: str = "unknown",
        vix_bucket: str = "sweet_spot",
        halted: bool = False,
        heartbeat: datetime | None = None,
        snapshot_db_path: Path | None = None,
        lots_db_path: Path | None = None,
        metrics_provider: Callable[[], "LiveMetrics"] | None = None,
    ) -> None:
        self._oms = oms_store
        self._memories = memories or {}
        self._calibration = calibration
        self._budget = budget
        self._master_capability = master_capability
        self._effective_max_gross = effective_max_gross
        self._regime_label = regime_label
        self._vix_bucket = vix_bucket
        self._halted = halted
        self._heartbeat = heartbeat
        self._snapshot_db_path: Path | None = (
            Path(snapshot_db_path) if snapshot_db_path is not None else None
        )
        self._lots_db_path: Path | None = (
            Path(lots_db_path) if lots_db_path is not None else None
        )
        # Optional callback that returns a fresh LiveMetrics on each top_strip()
        # call.  When supplied, overrides the static fallback values above.
        self._metrics_provider = metrics_provider

    # ── Top strip ────────────────────────────────────────────────────────────

    def top_strip(self) -> TopStripMetrics:
        spend = self._budget.today_spent() if self._budget else Decimal("0")
        limit = self._budget.daily_limit() if self._budget else Decimal("0.95")
        spend_pct = float(spend / limit * 100) if limit > 0 else 0.0
        if self._heartbeat is not None:
            age = int((datetime.now(UTC) - self._heartbeat).total_seconds())
        else:
            age = 0

        # Pull live values via the metrics_provider when supplied; otherwise
        # fall back to the static values stored at construction (used by
        # tests and the standalone _load_from_env() entry point).
        if self._metrics_provider is not None:
            try:
                live = self._metrics_provider()
                mc = live.master_capability
                emg = live.effective_max_gross
                vix_bucket = live.vix_bucket
                halted = live.halted
            except Exception:
                mc = self._master_capability
                emg = self._effective_max_gross
                vix_bucket = self._vix_bucket
                halted = self._halted
        else:
            mc = self._master_capability
            emg = self._effective_max_gross
            vix_bucket = self._vix_bucket
            halted = self._halted

        nav, day_pnl = self._nav_and_day_pnl()
        return TopStripMetrics(
            total_nav=nav,
            day_pnl_gross=day_pnl,
            day_spend_usd=spend,
            spend_limit_usd=limit,
            spend_pct=spend_pct,
            halted=halted,
            master_capability=mc,
            effective_max_gross=emg,
            regime_label=self._regime_label,
            vix_bucket=vix_bucket,
            heartbeat_age_s=age,
            approval_queue_count=0,
        )

    # ── Per-agent summary ────────────────────────────────────────────────────

    def agent_summary(self, agent_id: AgentId, n_intents: int = 5) -> AgentSummary:
        intents: list[IntentRow] = []
        mem = self._memories.get(agent_id)
        if mem is not None:
            for row in mem.recent_intents_rows(n_intents):
                intents.append(
                    IntentRow(
                        timestamp=str(row["logged_at"]),
                        agent_id=str(agent_id),
                        symbol=str(row["symbol"]),
                        action=str(row["action"]),
                        conviction=int(row["conviction"] or 0),
                        outcome=str(row["outcome"]) if row["outcome"] is not None else None,
                        rationale=str(row["rationale"] or "")[:200],
                    )
                )

        brier = 0.0
        cal_table: list[dict[str, Any]] = []
        if self._calibration is not None:
            brier = self._calibration.brier_score(agent_id=str(agent_id))
            cal_table = self._calibration.calibration_table(agent_id=str(agent_id))

        return AgentSummary(
            agent_id=str(agent_id),
            model=_AGENT_MODEL.get(agent_id, "unknown"),
            sleeve_equity=None,
            four_week_return_pct=None,
            recent_intents=intents,
            brier_score=brier,
            calibration_table=cal_table,
        )

    # ── Bottom-strip logs ────────────────────────────────────────────────────

    def recent_fills(self, n: int = 50) -> list[FillRow]:
        if self._oms is None:
            return []
        events = self._oms.recent_by_kind(EventKind.FILL_RECEIVED, n)

        # Build the intent_id → rationale lookup ONCE per call. The previous
        # implementation re-fetched every agent's last 200 intent rows for
        # every fill, which is N_fills × N_agents × 200 row scans per 3s
        # dashboard poll. We need at most one fetch per agent regardless of
        # how many fills are returned.
        rationale_by_intent: dict[str, str] = {}
        for aid, mem in self._memories.items():
            try:
                for row in mem.recent_intents_rows(200):
                    iid = str(row.get("intent_id") or "")
                    if iid:
                        rationale_by_intent[iid] = (
                            str(row.get("rationale") or "")[:200]
                        )
            except Exception:
                continue

        rows: list[FillRow] = []
        for ev in events:
            qty = _as_decimal(ev.payload.get("qty", 0))
            price = _as_decimal(ev.payload.get("price", 0))
            agent_id, rationale = self._lookup_intent_for_order(
                ev.order_id, rationale_by_intent,
            )
            rows.append(
                FillRow(
                    timestamp=ev.ts.isoformat(),
                    order_id=str(ev.order_id),
                    symbol=str(ev.payload.get("symbol", "")),
                    side=str(ev.payload.get("side", "")),
                    qty=qty,
                    price=price,
                    total_cost=qty * price,
                    agent_id=agent_id,
                    rationale=rationale,
                )
            )
        return rows

    def _lookup_intent_for_order(
        self,
        order_id: Any,
        rationale_by_intent: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Walk the order's events for the submit_intent and pull agent_id +
        rationale via the prebuilt map (or fall back to a per-agent scan
        when callers don't provide one)."""
        if self._oms is None:
            return "", ""
        intent_id: str = ""
        agent_id: str = ""
        try:
            for ev in self._oms.iter_for_order(order_id):
                if ev.kind == EventKind.ORDER_SUBMIT_INTENT:
                    raw_iid = ev.payload.get("intent_id")
                    if isinstance(raw_iid, dict):
                        intent_id = str(raw_iid.get("__uuid__", ""))
                    else:
                        intent_id = str(raw_iid or "")
                    agent_id = str(ev.payload.get("agent_id", ""))
                    break
        except Exception:
            return "", ""
        if not intent_id or not agent_id:
            return agent_id, ""
        if rationale_by_intent is not None:
            return agent_id, rationale_by_intent.get(intent_id, "")
        for aid, mem in self._memories.items():
            short = str(aid).split(".")[-1].lower()
            if short != agent_id.lower():
                continue
            try:
                for row in mem.recent_intents_rows(200):
                    if str(row.get("intent_id")) == intent_id:
                        return agent_id, str(row.get("rationale") or "")[:200]
            except Exception:
                return agent_id, ""
        return agent_id, ""

    def recent_intents(self, n: int = 50) -> list[IntentRow]:
        all_rows: list[IntentRow] = []
        for agent_id, mem in self._memories.items():
            for row in mem.recent_intents_rows(n):
                all_rows.append(
                    IntentRow(
                        timestamp=str(row["logged_at"]),
                        agent_id=str(agent_id),
                        symbol=str(row["symbol"]),
                        action=str(row["action"]),
                        conviction=int(row["conviction"] or 0),
                        outcome=str(row["outcome"]) if row["outcome"] is not None else None,
                        rationale=str(row["rationale"] or "")[:200],
                    )
                )
        all_rows.sort(key=lambda r: r.timestamp, reverse=True)
        return all_rows[:n]

    # ── Spend gauge ──────────────────────────────────────────────────────────

    def spend_breakdown(self, fraction_of_day_elapsed: float = 0.5) -> SpendBreakdown:
        if self._budget is None:
            return SpendBreakdown(
                today_total=Decimal("0"),
                daily_limit=Decimal("0.95"),
                by_call_type={},
                by_agent={},
                eod_forecast=Decimal("0"),
            )

        today_total = self._budget.today_spent()
        limit = self._budget.daily_limit()
        entries: list[Any] = self._budget.entries()

        by_call_type: dict[str, Decimal] = {}
        by_agent: dict[str, Decimal] = {}
        for e in entries:
            ct = str(e.get("call_type", "unknown"))
            ag = str(e.get("agent_id", "unknown"))
            cost = _as_decimal(e.get("cost_usd", 0))
            by_call_type[ct] = by_call_type.get(ct, Decimal("0")) + cost
            by_agent[ag] = by_agent.get(ag, Decimal("0")) + cost

        if fraction_of_day_elapsed > 0.01:
            forecast = today_total / Decimal(str(fraction_of_day_elapsed))
        else:
            forecast = today_total
        forecast = min(forecast, limit * Decimal("2"))

        return SpendBreakdown(
            today_total=today_total,
            daily_limit=limit,
            by_call_type=by_call_type,
            by_agent=by_agent,
            eod_forecast=forecast,
        )


    # ── Snapshot-DB time-series reads ────────────────────────────────────────

    def _open_snapshot_ro(self) -> sqlite3.Connection | None:
        """Open the snapshot DB read-only. Returns None if unavailable."""
        if self._snapshot_db_path is None or not self._snapshot_db_path.exists():
            return None
        try:
            uri = f"file:{self._snapshot_db_path}?mode=ro"
            return sqlite3.connect(uri, uri=True)
        except Exception:
            return None

    def _nav_and_day_pnl(self) -> tuple[Decimal | None, Decimal | None]:
        """Latest NAV from snapshot DB, plus day P&L = latest - first-of-day."""
        conn = self._open_snapshot_ro()
        if conn is None:
            return (None, None)
        try:
            latest = conn.execute(
                "SELECT ts, total_nav FROM equity_snapshots "
                "WHERE total_nav IS NOT NULL ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                return (None, None)
            nav = _as_decimal(latest[1])
            today_prefix = str(latest[0])[:10]
            first = conn.execute(
                "SELECT total_nav FROM equity_snapshots "
                "WHERE total_nav IS NOT NULL AND substr(ts,1,10) = ? "
                "ORDER BY ts ASC LIMIT 1",
                (today_prefix,),
            ).fetchone()
            day_pnl = nav - _as_decimal(first[0]) if first is not None else None
            return (nav, day_pnl)
        except sqlite3.Error:
            return (None, None)
        finally:
            conn.close()

    def agent_pnl_recent(self, limit: int = 10) -> list[dict[str, object]]:
        """Most recent N days of per-agent P&L attribution snapshots (T1.5).

        Returns one dict per (date, agent) row, newest first. Returns []
        when the snapshot DB or the agent_pnl_daily table is missing.
        """
        conn = self._open_snapshot_ro()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT date, agent_id, realized, unrealized, total, "
                "num_open, num_closed FROM agent_pnl_daily "
                "ORDER BY date DESC, agent_id ASC LIMIT ?",
                (limit * 4,),  # 4 agents * limit days
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        out: list[dict[str, object]] = []
        for d, aid, real, unreal, total, nopen, nclosed in rows:
            out.append(
                {
                    "date": str(d),
                    "agent_id": str(aid),
                    "realized": float(_as_decimal(real)),
                    "unrealized": float(_as_decimal(unreal)),
                    "total": float(_as_decimal(total)),
                    "num_open": int(nopen),
                    "num_closed": int(nclosed),
                }
            )
        return out

    def sleeve_curves(self) -> list[SleeveCurvePoint]:
        """Time-series of per-agent sleeve equity, melted into one point per row."""
        conn = self._open_snapshot_ro()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT ts, haiku_equity, sonnet_equity, opus_equity, manager_equity "
                "FROM equity_snapshots ORDER BY ts ASC"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

        points: list[SleeveCurvePoint] = []
        for ts, h, s, o, m in rows:
            for agent, val in (("haiku", h), ("sonnet", s), ("opus", o), ("manager", m)):
                if val is None:
                    continue
                points.append(SleeveCurvePoint(ts=ts, agent_id=agent, equity=_as_decimal(val)))
        return points

    def nav_curve(self) -> list[NavPoint]:
        """Time-series of total NAV from the broker account snapshot."""
        conn = self._open_snapshot_ro()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT ts, total_nav FROM equity_snapshots ORDER BY ts ASC"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [
            NavPoint(ts=ts, total_nav=_as_decimal(nav) if nav is not None else None)
            for ts, nav in rows
        ]

    def spend_curve(self) -> list[SpendPoint]:
        """Cumulative LLM spend over today's entries (ordered by ts)."""
        if self._budget is None:
            return []
        try:
            entries = list(self._budget.entries())
        except Exception:
            return []
        entries.sort(key=lambda e: str(e.get("ts", "")))
        running = Decimal("0")
        out: list[SpendPoint] = []
        for e in entries:
            running += _as_decimal(e.get("cost_usd", 0))
            out.append(SpendPoint(ts=str(e.get("ts", "")), cumulative_usd=running))
        return out

    def calibration_scatter(self) -> list[CalibrationPoint]:
        """Per-agent, per-conviction-bucket realized win rate (1..10)."""
        if self._calibration is None:
            return []
        # CalibrationTracker stores raw conviction integers; query directly
        # (read-only) so we can bucket at the 1..10 grain instead of the
        # coarse low/medium/high buckets exposed by calibration_table().
        try:
            rows = self._calibration._conn.execute(  # noqa: SLF001
                "SELECT agent_id, conviction, outcome FROM calibration"
            ).fetchall()
        except Exception:
            return []

        # bucket -> (n, wins)
        agg: dict[tuple[str, int], list[int]] = {}
        for row in rows:
            agent_id = str(row["agent_id"]) if hasattr(row, "keys") else str(row[0])
            conviction = int(row["conviction"]) if hasattr(row, "keys") else int(row[1])
            outcome = str(row["outcome"]) if hasattr(row, "keys") else str(row[2])
            key = (agent_id, conviction)
            entry = agg.setdefault(key, [0, 0])
            entry[0] += 1
            if outcome == "win":
                entry[1] += 1

        out: list[CalibrationPoint] = []
        for (agent_id, bucket), (n, wins) in agg.items():
            win_rate = wins / n if n > 0 else 0.0
            out.append(CalibrationPoint(
                agent_id=agent_id,
                conviction_bucket=bucket,
                win_rate=win_rate,
                n=n,
            ))
        return out

    def agent_performance(self, agent_short_id: str) -> AgentPerformance:
        """4-week Sharpe/Sortino/maxDD from per-agent equity series + win/loss
        from the calibration table. Risk-free rate assumed 4.5% annualized."""
        from math import sqrt

        rf_annual = 0.045
        rf_daily = rf_annual / 252.0

        equity_col = f"{agent_short_id.lower()}_equity"
        series: list[float] = []
        conn = self._open_snapshot_ro()
        if conn is not None:
            try:
                cutoff = (datetime.now(UTC) - timedelta(days=28)).isoformat()
                rows = conn.execute(
                    f"SELECT {equity_col} FROM equity_snapshots "
                    f"WHERE ts >= ? AND {equity_col} IS NOT NULL "
                    f"ORDER BY ts ASC",
                    (cutoff,),
                ).fetchall()
                for (raw,) in rows:
                    try:
                        v = float(_as_decimal(raw))
                    except Exception:
                        continue
                    if v > 0:
                        series.append(v)
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        # Daily returns from successive equity points.
        rets = [
            (series[i] - series[i - 1]) / series[i - 1]
            for i in range(1, len(series))
            if series[i - 1] > 0
        ]

        sharpe: float | None = None
        sortino: float | None = None
        if len(rets) >= 5:
            mean_excess = sum(r - rf_daily for r in rets) / len(rets)
            var = sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / len(rets)
            sd = sqrt(var)
            if sd > 0:
                sharpe = (mean_excess / sd) * sqrt(252)
            downside = [r - rf_daily for r in rets if r - rf_daily < 0]
            # Require ≥3 downside samples; with fewer, Sortino's denominator
            # is too small and the ratio reads as a wildly optimistic outlier.
            if len(downside) >= 3:
                dvar = sum(r * r for r in downside) / len(downside)
                ddev = sqrt(dvar)
                if ddev > 0:
                    sortino = (mean_excess / ddev) * sqrt(252)

        # Max drawdown over the 4-week window.
        max_dd = 0.0
        if len(series) >= 2:
            peak = series[0]
            for v in series:
                peak = max(peak, v)
                dd = (v - peak) / peak
                if dd < max_dd:
                    max_dd = dd

        # Win/loss stats from the calibration table for this agent.
        win_rate: float | None = None
        loss_rate: float | None = None
        n_closed = 0
        if self._calibration is not None:
            try:
                table = self._calibration.calibration_table(agent_id=agent_short_id)
                wins = 0
                losses = 0
                for bucket in table:
                    n = int(bucket.get("n") or 0)
                    if not n:
                        continue
                    n_closed += n
                    wr = bucket.get("win_rate")
                    if wr is not None:
                        wins += int(round(float(wr) * n))
                if n_closed:
                    losses = n_closed - wins  # flat lumped with loss for rate calc
                    win_rate = wins / n_closed
                    loss_rate = losses / n_closed
            except Exception:
                pass

        return AgentPerformance(
            sharpe_4w=sharpe,
            sortino_4w=sortino,
            max_dd_4w=max_dd,
            win_rate=win_rate,
            loss_rate=loss_rate,
            n_closed=n_closed,
        )

    def drawdown_status(self) -> list[DrawdownPoint]:
        """Per-agent (peak - current) / peak from the latest equity snapshot."""
        conn = self._open_snapshot_ro()
        if conn is None:
            return []
        try:
            row = conn.execute(
                "SELECT haiku_equity, sonnet_equity, opus_equity, manager_equity, "
                "haiku_peak, sonnet_peak, opus_peak, manager_peak "
                "FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        if row is None:
            return []

        agents = ("haiku", "sonnet", "opus", "manager")
        out: list[DrawdownPoint] = []
        for i, agent in enumerate(agents):
            cur = _as_decimal(row[i]) if row[i] is not None else Decimal("0")
            peak = _as_decimal(row[i + 4]) if row[i + 4] is not None else Decimal("0")
            if peak > 0:
                dd = float((peak - cur) / peak)
            else:
                dd = 0.0
            dd = max(dd, 0.0)
            if dd < 0.05:
                bucket = "ok"
            elif dd < 0.10:
                bucket = "halved"
            elif dd < 0.15:
                bucket = "warning"
            else:
                bucket = "halt"
            out.append(DrawdownPoint(agent_id=agent, drawdown_pct=dd, bucket=bucket))
        return out

    def current_positions(self) -> list[PositionRow]:
        """Latest open-position row-set (rows where ts == max(ts))."""
        conn = self._open_snapshot_ro()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT symbol, qty, market_value, side, unrealized_pl "
                "FROM position_snapshots "
                "WHERE ts = (SELECT MAX(ts) FROM position_snapshots)"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [
            PositionRow(
                symbol=str(sym),
                qty=_as_decimal(qty),
                market_value=_as_decimal(mv) if mv is not None else Decimal("0"),
                side=str(side or ""),
                unrealized_pl=_as_decimal(upl) if upl is not None else None,
            )
            for sym, qty, mv, side, upl in rows
        ]

    def current_positions_by_agent(self) -> dict[str, list[PositionRow]]:
        """Open positions attributed per agent, derived from the FIFO lot ledger.

        Aggregates open lots by (agent_id, symbol) for qty and weighted-avg
        cost basis, then joins to the latest position_snapshots row for marks.
        Returns {agent_short_id: [PositionRow, ...]}.
        """
        if self._lots_db_path is None or not self._lots_db_path.exists():
            return {}

        try:
            conn = sqlite3.connect(f"file:{self._lots_db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return {}
        try:
            lot_rows = conn.execute(
                "SELECT agent_id, symbol, "
                "       SUM(CAST(remaining_qty AS REAL)) AS qty, "
                "       SUM(CAST(remaining_qty AS REAL) * CAST(entry_price AS REAL)) AS cost "
                "FROM lots "
                "WHERE is_closed = 0 AND CAST(remaining_qty AS REAL) > 0 "
                "GROUP BY agent_id, symbol"
            ).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

        marks: dict[str, Decimal] = {}
        snap = self._open_snapshot_ro()
        if snap is not None:
            try:
                for sym, qty, mv in snap.execute(
                    "SELECT symbol, qty, market_value FROM position_snapshots "
                    "WHERE ts = (SELECT MAX(ts) FROM position_snapshots)"
                ).fetchall():
                    q = _as_decimal(qty)
                    if q == 0 or mv is None:
                        continue
                    marks[str(sym)] = _as_decimal(mv) / q
            except sqlite3.Error:
                pass
            finally:
                snap.close()

        out: dict[str, list[PositionRow]] = {}
        for agent_id, symbol, qty_f, cost_f in lot_rows:
            if qty_f is None or float(qty_f) <= 0:
                continue
            qty = Decimal(str(qty_f))
            cost = Decimal(str(cost_f or 0))
            mark = marks.get(str(symbol))
            if mark is not None:
                mv = mark * qty
                upl: Decimal | None = mv - cost
            else:
                mv = cost
                upl = None
            short = str(agent_id).split(".")[-1].lower()
            out.setdefault(short, []).append(
                PositionRow(
                    symbol=str(symbol),
                    qty=qty,
                    market_value=mv,
                    side="long",
                    unrealized_pl=upl,
                )
            )
        for rows in out.values():
            rows.sort(key=lambda r: r.market_value, reverse=True)
        return out

    def agent_positions_history(
        self,
        agent_id: str,
        since_iso: str | None = None,
    ) -> list[tuple[str, str, Decimal, Decimal | None]]:
        """Time series of (ts, symbol, qty, market_value) for one agent.

        Reads from the `agent_position_snapshots` table populated by
        EquitySnapshotter. Used by per-agent position-history charts that the
        aggregate `position_snapshots` table cannot answer.

        since_iso: optional ISO timestamp lower bound (inclusive). Pass None
        to get the full retained history (per the snapshotter prune policy).
        """
        conn = self._open_snapshot_ro()
        if conn is None:
            return []
        try:
            sql = (
                "SELECT ts, symbol, qty, market_value "
                "FROM agent_position_snapshots "
                "WHERE agent_id = ?"
            )
            params: tuple[Any, ...] = (agent_id,)
            if since_iso is not None:
                sql += " AND ts >= ?"
                params = (agent_id, since_iso)
            sql += " ORDER BY ts ASC, symbol ASC"
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [
            (
                str(ts),
                str(sym),
                _as_decimal(qty),
                _as_decimal(mv) if mv is not None else None,
            )
            for ts, sym, qty, mv in rows
        ]


def _as_decimal(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        try:
            return Decimal(v)
        except Exception:
            return Decimal("0")
    if isinstance(v, dict) and "__decimal__" in v:
        return Decimal(str(v["__decimal__"]))
    return Decimal("0")
