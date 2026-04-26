"""Read-only data adapter for the dashboard.

Aggregates from OMSStore, AgentMemory(s), CalibrationTracker, and BudgetLedger.
Never mutates state. Per blueprint Principle: "the dashboard is read-only.
It polls SQLite/DuckDB every 3s; it never mutates state and is not on the
trading code path."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from agents.calibration import CalibrationTracker
from agents.memory import AgentMemory
from core.types import AgentId
from execution.budget import BudgetLedger
from execution.oms_store import EventKind, OMSStore

# ─── Dataclasses for each panel ───────────────────────────────────────────────


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


@dataclass(frozen=True)
class SpendBreakdown:
    today_total: Decimal
    daily_limit: Decimal
    by_call_type: dict[str, Decimal]
    by_agent: dict[str, Decimal]
    eod_forecast: Decimal


@dataclass(frozen=True)
class AgentSummary:
    agent_id: str
    model: str
    sleeve_equity: Decimal | None
    four_week_return_pct: Decimal | None
    recent_intents: list[IntentRow] = field(default_factory=list)
    brier_score: float = 0.0
    calibration_table: list[dict[str, Any]] = field(default_factory=list)


# ─── Adapter ──────────────────────────────────────────────────────────────────


_AGENT_MODEL: dict[AgentId, str] = {
    AgentId.HAIKU: "claude-haiku-4-5",
    AgentId.SONNET: "claude-sonnet-4-6",
    AgentId.OPUS: "claude-opus-4-7",
    AgentId.MANAGER: "claude-sonnet-4-6",
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

    # ── Top strip ────────────────────────────────────────────────────────────

    def top_strip(self) -> TopStripMetrics:
        spend = self._budget.today_spent() if self._budget else Decimal("0")
        limit = self._budget.daily_limit() if self._budget else Decimal("0.95")
        spend_pct = float(spend / limit * 100) if limit > 0 else 0.0
        if self._heartbeat is not None:
            age = int((datetime.now(UTC) - self._heartbeat).total_seconds())
        else:
            age = 0
        return TopStripMetrics(
            total_nav=None,
            day_pnl_gross=None,
            day_spend_usd=spend,
            spend_limit_usd=limit,
            spend_pct=spend_pct,
            halted=self._halted,
            master_capability=self._master_capability,
            effective_max_gross=self._effective_max_gross,
            regime_label=self._regime_label,
            vix_bucket=self._vix_bucket,
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
        return [
            FillRow(
                timestamp=ev.ts.isoformat(),
                order_id=str(ev.order_id),
                symbol=str(ev.payload.get("symbol", "")),
                side=str(ev.payload.get("side", "")),
                qty=_as_decimal(ev.payload.get("qty", 0)),
                price=_as_decimal(ev.payload.get("price", 0)),
            )
            for ev in events
        ]

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
