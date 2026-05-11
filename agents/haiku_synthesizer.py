"""HaikuSynthesizer — daily 08:30 ET "junior Manager" morning brief (T2.3 / Plan 2c).

Replaces the prior Manager-on-Opus morning_brief job at ~50× the cost.
Reads four input streams (positions, last-week per-sleeve P&L, top-5
recent high-impact news, current VIX bucket), calls Haiku on a 4,459-
token system prompt, and persists the resulting markdown brief via
manager_bridge.write_morning_brief so all three sleeve agents see it
verbatim on their next observe().
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from agents.llm import BudgetExhausted, LLMClient
from agents.manager_bridge import write_morning_brief
from agents.memory import AgentMemory
from core.types import AgentId, NewsItem, VixBucket
from data.news_store import NewsStore

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "haiku_synthesizer.md"

# Match the synthesizer prompt's "prior 18 hours" rule for news inclusion.
_NEWS_LOOKBACK_HOURS: int = 18
_NEWS_MIN_IMPACT: int = 3
_NEWS_LIMIT: int = 5
# 7 trading days; we use calendar days since agent_pnl_daily is one row/day.
_PNL_LOOKBACK_DAYS: int = 7


def _holdings_block(positions_by_agent: dict[AgentId, list[tuple[str, Decimal]]]) -> str:
    """Format per-sleeve holdings for the synthesizer user message."""
    lines: list[str] = ["Holdings snapshot (per sleeve):"]
    for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
        positions = positions_by_agent.get(aid, [])
        if not positions:
            lines.append(f"  {aid.value}: flat (no holdings)")
        else:
            parts = ", ".join(f"{sym} {float(qty):g}" for sym, qty in positions[:8])
            lines.append(f"  {aid.value}: {parts}")
    return "\n".join(lines)


def _pnl_block(pnl_rows: dict[AgentId, dict[str, Decimal | int]]) -> str:
    """Format the prior-week per-sleeve P&L attribution summary."""
    lines: list[str] = [f"Last {_PNL_LOOKBACK_DAYS}d per-sleeve P&L (from agent_pnl_daily):"]
    for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
        row = pnl_rows.get(aid)
        if row is None:
            lines.append(f"  {aid.value}: no snapshots yet")
            continue
        lines.append(
            f"  {aid.value}: realized=${row['realized']} "
            f"unrealized=${row['unrealized']} "
            f"closed_lots={row['closed_lots']}"
        )
    return "\n".join(lines)


def _news_block(items: list[NewsItem], scored_map: dict[str, tuple[int, str]]) -> str:
    """Top-5 high-impact items, formatted compactly. scored_map: url -> (impact, surprise)."""
    if not items:
        return "Top news (last 18h, impact >= 3): (no items met threshold)"
    lines = [f"Top news (last 18h, impact >= 3, max {_NEWS_LIMIT}):"]
    for item in items[:_NEWS_LIMIT]:
        impact, surprise = scored_map.get(item.url, (0, "?"))
        syms = ",".join(item.symbols[:3]) if item.symbols else "—"
        when = item.published_at.strftime("%m-%d %H:%M")
        head = item.headline.strip().replace("\n", " ")
        if len(head) > 140:
            head = head[:137] + "..."
        lines.append(
            f"  [{when}] [{syms}] impact={impact} surprise={surprise}: {head}"
        )
    return "\n".join(lines)


def _vix_block(vix_value: Decimal | None, vix_bucket: VixBucket | None) -> str:
    bucket = vix_bucket.value.upper() if vix_bucket is not None else "UNKNOWN"
    if vix_value is not None:
        return f"VIX: bucket={bucket}, level={float(vix_value):.1f}"
    return f"VIX: bucket={bucket}, level=n/a"


def _query_recent_pnl(
    snapshot_db_path: Path | str, lookback_days: int = _PNL_LOOKBACK_DAYS,
) -> dict[AgentId, dict[str, Decimal | int]]:
    """Aggregate the last N days of agent_pnl_daily by agent. Read-only."""
    db_path = Path(snapshot_db_path)
    if not db_path.exists():
        return {}
    cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).date().isoformat()
    out: dict[AgentId, dict[str, Decimal | int]] = {}
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as con:
            rows = con.execute(
                "SELECT agent_id, "
                "  SUM(CAST(realized AS REAL)) AS r, "
                "  SUM(CAST(unrealized AS REAL)) AS u, "
                "  SUM(num_closed) AS c "
                "FROM agent_pnl_daily WHERE date >= ? GROUP BY agent_id",
                (cutoff,),
            ).fetchall()
    except sqlite3.Error:
        log.warning("haiku_synthesizer: agent_pnl_daily query failed", exc_info=True)
        return {}
    for aid_str, r, u, c in rows:
        try:
            aid = AgentId(str(aid_str))
        except ValueError:
            continue
        out[aid] = {
            "realized": Decimal(str(round(r or 0.0, 2))),
            "unrealized": Decimal(str(round(u or 0.0, 2))),
            "closed_lots": int(c or 0),
        }
    return out


class HaikuSynthesizer:
    """Daily 08:30 ET morning brief composer; replaces JOB_MANAGER_MORNING_BRIEF."""

    def __init__(
        self,
        llm: LLMClient,
        manager_memory: AgentMemory,
        news_store: NewsStore,
        snapshot_db_path: Path | str,
    ) -> None:
        self._llm = llm
        self._manager_memory = manager_memory
        self._news_store = news_store
        self._snapshot_db_path = Path(snapshot_db_path)
        self._prompt = _PROMPT_PATH.read_text()

    def synthesize(
        self,
        positions_by_agent: dict[AgentId, list[tuple[str, Decimal]]],
        vix_value: Decimal | None,
        vix_bucket: VixBucket | None,
        now: datetime | None = None,
    ) -> str | None:
        """Build inputs, call Haiku, persist the brief. Returns the brief text or None."""
        now = now or datetime.now(UTC)
        pnl_rows = _query_recent_pnl(self._snapshot_db_path)
        items, scored_map = self._top_news(now)

        user_msg = "\n\n".join([
            f"=== Morning synthesis @ {now.isoformat()} ===",
            _vix_block(vix_value, vix_bucket),
            _holdings_block(positions_by_agent),
            _pnl_block(pnl_rows),
            _news_block(items, scored_map),
            "Compose a 180-260 word markdown brief per the system prompt schema.",
        ])

        try:
            response_text, _ = self._llm.call(
                system=self._prompt,
                user=user_msg,
                agent_id=AgentId.HAIKU,
                call_type="morning_synthesis",
                max_tokens=512,
            )
        except BudgetExhausted:
            log.warning("HaikuSynthesizer: budget exhausted; skipping")
            return None
        except Exception:
            log.warning("HaikuSynthesizer: LLM call failed", exc_info=True)
            return None

        brief = response_text.strip()
        if not brief:
            log.warning("HaikuSynthesizer: empty response; skipping persist")
            return None

        try:
            write_morning_brief(self._manager_memory, brief)
        except Exception:
            log.warning("HaikuSynthesizer: write_morning_brief failed", exc_info=True)
        log.info("HaikuSynthesizer: brief written (%d chars)", len(brief))
        return brief

    def _top_news(
        self, now: datetime,
    ) -> tuple[list[NewsItem], dict[str, tuple[int, str]]]:
        """Pull the top-N scored items from the news_store for the prior 18h."""
        since = now - timedelta(hours=_NEWS_LOOKBACK_HOURS)
        try:
            triples = self._news_store.top_impact_since(
                since=since, min_impact=_NEWS_MIN_IMPACT, limit=_NEWS_LIMIT,
            )
        except Exception:
            log.warning("HaikuSynthesizer: top_impact_since query failed", exc_info=True)
            return [], {}
        items = [t[0] for t in triples]
        scored_map = {t[0].url: (t[1], t[2]) for t in triples}
        return items, scored_map


def positions_from_lot_ledger(
    lot_ledger: object,
) -> dict[AgentId, list[tuple[str, Decimal]]]:
    """Convenience: shape app.lots into the per-agent {symbol: qty} mapping the
    synthesizer expects. Tolerant of failures so the morning brief never blocks
    on lot-ledger plumbing.
    """
    out: dict[AgentId, list[tuple[str, Decimal]]] = {
        AgentId.HAIKU: [], AgentId.SONNET: [], AgentId.OPUS: [],
    }
    try:
        for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
            mapping = lot_ledger.open_qty_by_symbol(aid)  # type: ignore[attr-defined]
            out[aid] = sorted(mapping.items(), key=lambda kv: kv[0])
    except Exception:
        log.warning("positions_from_lot_ledger failed", exc_info=True)
    return out


__all__ = ["HaikuSynthesizer", "positions_from_lot_ledger"]
