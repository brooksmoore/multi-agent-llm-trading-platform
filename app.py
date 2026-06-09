"""Main entrypoint — orchestrates all four agents, OMS, broker, scheduler.

Run with: `python app.py` from the project root.

Architecture:
    App
    ├── singletons: EventBus, KillSwitchEngine, OMSStore, OMS, RiskGate,
    │              BudgetLedger, BudgetWatcher, LotLedger, WashSaleChecker,
    │              MarketData, Broker, four LLMClients, four AgentMemory dbs,
    │              HaikuAgent, SonnetAgent, OpusAgent, ManagerAgent
    ├── threads:    Reconciler (per-broker interval; 60s Alpaca / 20s Robinhood),
    │               HeartbeatWriter (30s), VolatilityScanner (300s during market hours),
    │               dashboard, BudgetWatcher (30s)
    └── scheduler:  BackgroundScheduler with all blueprint §2 cron jobs

Lifecycle:
    SIGINT / SIGTERM → graceful shutdown → flushes OMS log, snapshots
    memories, writes logs/shutdown_TIMESTAMP.json, exits 0.

Crash recovery:
    OMS.recover() replays the append-only event log on every startup, so
    SIGKILL is safe — no state is lost beyond the in-flight broker call,
    which the reconciler closes on the next poll tick (broker-specific interval).
"""

from __future__ import annotations

import json
import logging
import signal
import uuid
from dataclasses import replace
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config.universes import PLUMBING_UNIVERSE
from agents.base import AgentState, BaseAgent
from agents.calibration import CalibrationTracker
from agents.calibration_recorder import CalibrationRecorder
from agents.haiku_agent import HaikuAgent
from agents.manager_bridge import (
    read_manager_context,
    write_adversarial_critique,
    write_sleeve_weights,
)
from agents.llm import HAIKU_MODEL, OPUS_MODEL, SONNET_MODEL, BudgetExhausted, LLMClient
from agents.manager_agent import ManagerAgent, compute_manager_fingerprint
from agents.news_scorer import NewsScorer
from agents.haiku_synthesizer import HaikuSynthesizer, positions_from_lot_ledger
from agents.memory import AgentMemory
from agents.opus_agent import OpusAgent
from agents.outcome_recorder import OutcomeRecorder
from agents.sonnet_agent import SonnetAgent
from config.runtime_store import runtime_store
from config.settings import Settings
from core.clock import ET, Clock, WallClock
from core.events import EventBus, FillReceivedEvent
from core.types import (
    Action,
    AgentId,
    DrawdownBucket,
    Intent,
    KillSwitchState,
    MarketSnapshot,
    Order,
    OrderClass,
    OrderSide,
    OrderState,
    OrderType,
    Sleeve,
    TimeInForce,
    VixBucket,
    new_id,
    normalize_symbol,
)
from core.types import (
    AgentState as CoreAgentState,
)
from execution.agent_state_tracker import AgentStateTracker
from execution.broker import Broker, BrokerAccount
from execution.budget import BudgetLedger, BudgetWatcher
from execution.kill_switch import KillSwitchEngine
from execution.lots import LotLedger
from execution.oms import OMS
from execution.oms_store import OMSStore
from execution.planner import ExecutionPlanner
from execution.reconciler import Reconciler
from execution.risk import RiskGate
from execution.sizing import classify_vix, effective_max_gross
from execution.tax import WashSaleChecker
from data.doc_pack import build_doc_pack
from data.news import EDGARAdapter, FinnhubAdapter, RSSAdapter, YFinanceAdapter
from data.news_fetcher import DEFAULT_RSS_FEEDS, NewsFetcher
from data.news_store import NewsStore, default_retention_cutoff
from ops.agent_pnl_store import AgentPnLStore
from ops.alerts import AlertManager
from ops.attribution import compute_daily_pnl
from ops.manager_analytics import build_manager_context
from ops.equity_snapshotter import EquitySnapshotter
from ops.heartbeat import HeartbeatWriter
from ops.telegram import TelegramAdapter

if TYPE_CHECKING:
    from data.market import MarketData

log = logging.getLogger(__name__)

# ─── Defaults ─────────────────────────────────────────────────────────────────

# Plumbing universe = union of every sleeve's tradable list. Each agent
# applies its own strategy filter at the prompt/factor layer (see
# config/universes.py and agents/*_agent.py).
DEFAULT_UNIVERSE: list[str] = list(PLUMBING_UNIVERSE)

# ─── Scheduler job IDs (used by tests and introspection) ─────────────────────

JOB_HAIKU_NEWS_SCAN          = "haiku_news_scan"          # 13:30 ET
JOB_HAIKU_CLOSE              = "haiku_close"              # 15:55 ET
# Plan 2c: only Sonnet job/day. 12-1 momentum on daily bars cannot change intraday.
JOB_SONNET_EOD               = "sonnet_eod"               # 16:30 ET
# Plan 2c: removed JOB_OPUS_DAILY and JOB_OPUS_FRIDAY_DEEPDIVE.
# Opus runs once weekly as a deep dive on Thursday, giving Friday's
# Manager journal fresh input. Off-schedule deep dives still fire
# event-driven on NewsHighImpactScoredEvent (T2.5), rate-limited.
JOB_OPUS_THURSDAY_DEEPDIVE   = "opus_thursday_deepdive"   # Thu 16:30 ET
# Plan 2c follow-up: Opus.observe() was orphaned when JOB_OPUS_DAILY was
# removed (commit 4425450). Restored on a once-weekly Monday cadence so
# Opus can refresh its watchlist and emit starter intents while holdings
# are below TARGET_HOLDINGS. Single weekly call at Opus pricing stays
# inside the $0.10/day cap.
JOB_OPUS_MONDAY_INIT         = "opus_monday_init"         # Mon 09:35 ET
# Path A (2026-05-28): accelerate Opus capital deployment. Self-guarded —
# only dispatches while open holdings < TARGET_HOLDINGS, then self-disables.
# Fixes ~30% of NAV sitting idle in the Opus sleeve, which lags SPY on up days.
JOB_OPUS_DAILY_INIT          = "opus_daily_init"          # Mon-Fri 10:00 ET (until built)
# M1: Manager owns a SPY reserve position so the unallocated capital earns
# market beta instead of structurally underperforming the benchmark. The
# job is idempotent — checks the lot ledger first and only buys if MANAGER
# has no open SPY. Runs weekly so a flatten/wipe self-heals on the next
# Monday rather than requiring a manual restart.
JOB_MANAGER_RESERVE_CHECK    = "manager_reserve_check"    # Mon 09:40 ET
JOB_MANAGER_FRIDAY           = "manager_friday"           # Fri 17:00 ET
# T1.6 / T2.3: JOB_MANAGER_MORNING_BRIEF removed; replaced by JOB_HAIKU_MORNING_SYNTHESIS
JOB_HAIKU_MORNING_SYNTHESIS  = "haiku_morning_synthesis"  # Mon-Fri 08:30 ET
JOB_HAIKU_CRYPTO             = "haiku_crypto"             # 24/7, 60-min
JOB_BUDGET_RESET             = "budget_reset"             # UTC midnight
JOB_NEWS_FETCH               = "news_fetch"               # every 30 min during RTH
JOB_NEWS_NIGHTLY             = "news_nightly"             # 22:00 ET full pull + prune
JOB_PORTFOLIO_SNAPSHOT       = "portfolio_snapshot"       # hourly RTH Mon-Fri Telegram
JOB_PORTFOLIO_SNAPSHOT_WEEKEND = "portfolio_snapshot_weekend"  # 09:00 ET Sat/Sun
JOB_AGENT_PNL_SNAPSHOT       = "agent_pnl_snapshot"       # 16:45 ET Mon-Fri (T1.5)
JOB_MANAGER_SUNDAY_CRITIQUE  = "manager_sunday_critique"  # Sun 18:00 ET (T2.4)

ALL_JOB_IDS: frozenset[str] = frozenset({
    JOB_HAIKU_NEWS_SCAN, JOB_HAIKU_CLOSE,
    JOB_SONNET_EOD, JOB_OPUS_THURSDAY_DEEPDIVE, JOB_OPUS_MONDAY_INIT,
    JOB_OPUS_DAILY_INIT,
    JOB_MANAGER_RESERVE_CHECK,
    JOB_MANAGER_FRIDAY, JOB_HAIKU_MORNING_SYNTHESIS, JOB_HAIKU_CRYPTO,
    JOB_BUDGET_RESET, JOB_NEWS_FETCH, JOB_NEWS_NIGHTLY,
    JOB_PORTFOLIO_SNAPSHOT, JOB_PORTFOLIO_SNAPSHOT_WEEKEND,
    JOB_AGENT_PNL_SNAPSHOT, JOB_MANAGER_SUNDAY_CRITIQUE,
})


# Calendar-day window of daily bars loaded into every AgentState. Sized by the
# longest-lookback consumer: Sonnet's 12-1 momentum needs 252 + 21 ≈ 273 daily
# bars, which is ~397 calendar days once weekends/holidays are removed. Do NOT
# shrink this below ~400 without re-checking each agent's signal — Haiku's
# 210-day SMA and Sonnet's momentum both silently return None on short history,
# which looks like "agent went quiet" rather than an error. The upstream bar
# cache already serves this cheaply, so the window is not a live-cost concern.
_AGENT_STATE_LOOKBACK_DAYS = 400


# ─── App ──────────────────────────────────────────────────────────────────────


class App:
    """Single-process orchestrator owning every singleton.

    Construction does not start any threads — call `start()` explicitly. This
    keeps tests deterministic and lets `__init__` raise loudly on misconfig
    before any side effect.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        broker: Broker | None = None,
        market_data: MarketData | None = None,
        clock: Clock | None = None,
        oms_db_path: Path | None = None,
        budget_path: Path | None = None,
        memory_dir: Path | None = None,
        heartbeat_path: Path | None = None,
        logs_dir: Path | None = None,
        universe: list[str] | None = None,
        run_dashboard: bool = False,
        run_volatility_scanner: bool = False,
        run_recover_on_start: bool = True,
    ) -> None:
        self.settings = settings
        self.clock: Clock = clock if clock is not None else WallClock()
        self.universe = universe if universe is not None else DEFAULT_UNIVERSE
        self._run_dashboard = run_dashboard
        self._run_volatility_scanner = run_volatility_scanner
        self._run_recover_on_start = run_recover_on_start

        # Path setup ----------------------------------------------------------
        data_dir = Path(settings.data_dir)
        logs_dir = logs_dir if logs_dir is not None else Path(settings.logs_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir = logs_dir

        oms_db_path = oms_db_path if oms_db_path is not None else (data_dir / "oms.db")
        budget_path = budget_path if budget_path is not None else (data_dir / "daily_spend.json")
        memory_dir = memory_dir if memory_dir is not None else (data_dir / "memory")
        memory_dir.mkdir(parents=True, exist_ok=True)
        heartbeat_path = (
            heartbeat_path if heartbeat_path is not None else (logs_dir / "heartbeat.json")
        )
        self._oms_db_path = oms_db_path
        self._budget_path = budget_path
        self._memory_dir = memory_dir
        self._heartbeat_path = heartbeat_path
        self._snapshot_db_path = data_dir / "equity_snapshots.db"

        # Core singletons -----------------------------------------------------
        self.bus = EventBus()
        self.kill = KillSwitchEngine()
        self.lots = LotLedger(db_path=str(data_dir / "lots.db"))
        self.wash = WashSaleChecker()
        self.risk = RiskGate(self.kill, self.wash, self.lots, event_bus=self.bus)

        self.budget = BudgetLedger(self._budget_path, daily_limit=settings.daily_spend_cap)
        self.budget_watcher = BudgetWatcher(self.budget, self.kill, bus=self.bus)

        self.broker: Broker = broker if broker is not None else self._build_broker()
        self.market_data: MarketData = (
            market_data if market_data is not None else self._build_market_data()
        )

        self.store = OMSStore(self._oms_db_path)
        self.oms = OMS(self.broker, self.store, self.bus, clock=self.clock)

        # Backfill lots from any pre-existing OMS fill events on cold start.
        # Idempotent — safe to re-run; no-op once the lot DB has been populated.
        if self.lots.is_empty():
            try:
                booked = self.lots.replay_from_oms_store(self.store)
                if booked:
                    log.info("LotLedger: backfilled %d fills from OMS log", booked)
            except Exception:
                log.exception("LotLedger backfill from OMS store failed")

        tracker_db = str(data_dir / "agent_tracker.db")
        self.tracker = AgentStateTracker(
            kill_switch=self.kill,
            lot_ledger=self.lots,
            starting_equity=settings.starting_equity,
            db_path=tracker_db,
            bus=self.bus,
        )
        self.planner = ExecutionPlanner(self.oms, self.lots, self.bus)
        # Per-agent signal fingerprint for skip-when-unchanged gating.
        self._last_fingerprint: dict[AgentId, str] = {}
        # Manager strategic-call fingerprint (regime_read + weekly_journal).
        # Event-driven Manager calls are not gated by this; see T1.4 / Plan 2c.
        self._last_manager_fingerprint: str | None = None
        self.bus.subscribe("fill.received", self._on_fill_received)

        # Reconciler interval is chosen per broker_kind. Robinhood has no push
        # stream, so every fill awareness depends on this poll; default 20s.
        # Alpaca uses its WS stream as primary and 60s as safety net.
        kind = (settings.broker_kind or "alpaca").lower()
        rec_interval = (
            settings.reconciler_interval_robinhood_secs
            if kind == "robinhood"
            else settings.reconciler_interval_secs
        )
        self.reconciler = Reconciler(
            self.oms,
            self.broker,
            self.kill,
            interval_secs=rec_interval,
            qty_tolerance=settings.reconciler_qty_tolerance,
            bus=self.bus,
        )

        # LLM clients (one per agent so per-agent budgets/cache patterns stay distinct)
        api_key = settings.anthropic_api_key or None
        self._llm_haiku = LLMClient(self.budget, model=HAIKU_MODEL, api_key=api_key)
        self._llm_sonnet = LLMClient(self.budget, model=SONNET_MODEL, api_key=api_key)
        self._llm_opus = LLMClient(self.budget, model=OPUS_MODEL, api_key=api_key)
        self._llm_manager = LLMClient(self.budget, model=OPUS_MODEL, api_key=api_key)
        # T2.1: Sonnet-bound LLMClient for the Manager's risk_check_lite path
        # (budget-protective downgrade after the daily Opus risk_check ceiling).
        self._llm_manager_lite = LLMClient(
            self.budget, model=SONNET_MODEL, api_key=api_key,
        )
        # H2 (2026-05-28): Sonnet-bound client for Opus initiation book-building.
        # Initiation = "pick top-conviction names to get invested" — structured
        # enough for Sonnet. Opus model reserved for management-mode thesis review
        # and weekly deep-dives where the reasoning premium is earned.
        self._llm_opus_lite = LLMClient(
            self.budget, model=SONNET_MODEL, api_key=api_key,
        )

        # Agent memories (one SQLite db each)
        self._memories = {
            AgentId.HAIKU:   AgentMemory(memory_dir / "haiku.db",   AgentId.HAIKU),
            AgentId.SONNET:  AgentMemory(memory_dir / "sonnet.db",  AgentId.SONNET),
            AgentId.OPUS:    AgentMemory(memory_dir / "opus.db",    AgentId.OPUS),
            AgentId.MANAGER: AgentMemory(memory_dir / "manager.db", AgentId.MANAGER),
        }

        self.haiku = HaikuAgent(self._llm_haiku, self._memories[AgentId.HAIKU])
        self.sonnet = SonnetAgent(self._llm_sonnet, self._memories[AgentId.SONNET])
        self.opus = OpusAgent(
            self._llm_opus,
            self._memories[AgentId.OPUS],
            llm_lite=self._llm_opus_lite,
        )
        self.manager = ManagerAgent(
            self._llm_manager,
            self._memories[AgentId.MANAGER],
            llm_lite=self._llm_manager_lite,
        )

        # Routes terminal intent outcomes (filled / rejected / cancelled /
        # expired / vetoed / unsized) back to per-agent intent_log so the LLM
        # context can distinguish completed from silently-failed intents.
        self.outcome_recorder = OutcomeRecorder(self._memories, self.oms, self.bus)

        # Calibration: record win/loss/flat per opening intent every time a
        # SELL fill closes a lot. Without this hookup, calibration.db stays
        # empty and Brier scores show 0.00 forever.
        self.calibration = CalibrationTracker(str(data_dir / "calibration.db"))
        self.calibration_recorder = CalibrationRecorder(
            self.calibration, self.lots, self.store, self.bus,
            memories=self._memories,
        )

        # Manager drawdown responder: when AgentStateTracker fires a
        # DrawdownLadderFiredEvent (an agent transitions to a worse bucket),
        # call the Manager's drawdown_response and persist the directive so
        # the affected sleeve sees it on its next observe() cycle.
        self.bus.subscribe("drawdown.ladder_fired", self._on_drawdown_ladder_fired)
        # T2.5: off-schedule Opus deep dives triggered by high-impact news
        # on names Opus holds. Rate-limited to 1 extra dive per ISO week.
        self.bus.subscribe("news.high_impact_scored", self._on_news_high_impact)

        # News pipeline: persistence + adapters + orchestrator. Adapters are
        # constructed regardless of credentials — adapters with empty keys
        # silently return [] and the fetcher tolerates per-adapter failures.
        self.news_store = NewsStore(data_dir / "news.db")
        finnhub = FinnhubAdapter(settings.finnhub_api_key) if settings.finnhub_api_key else None
        self.news_fetcher = NewsFetcher(
            store=self.news_store,
            finnhub=finnhub,
            edgar=EDGARAdapter(),
            rss=RSSAdapter(DEFAULT_RSS_FEEDS),
            yfinance=YFinanceAdapter(),
        )
        # T2.2: Haiku-powered news-impact scoring. Runs after every news
        # fetch over the items that don't yet have a score. Cheap (~$0.0001
        # per scored item with cache hits) and pre-filtered by body+universe.
        self.news_scorer = NewsScorer(
            llm=self._llm_haiku, store=self.news_store, bus=self.bus,
        )
        # T2.3 / Plan 2c: HaikuSynthesizer composes the daily 08:30 ET
        # morning brief; replaces the prior Manager-on-Opus brief at ~50×
        # lower cost. Writes via manager_bridge.write_morning_brief so
        # sleeve agents see it verbatim on the next observe() cycle.
        self.haiku_synthesizer = HaikuSynthesizer(
            llm=self._llm_haiku,
            manager_memory=self._memories[AgentId.MANAGER],
            news_store=self.news_store,
            snapshot_db_path=self._snapshot_db_path,
        )

        # Ops
        self.heartbeat = HeartbeatWriter(self._heartbeat_path, kill=self.kill)
        self.equity_snapshotter = EquitySnapshotter(
            db_path=self._snapshot_db_path,
            agent_state_tracker=self.tracker,
            broker=self.broker,
            lot_ledger=self.lots,
        )
        # T1.5 / Plan 2c: per-sleeve P&L attribution snapshots written daily
        # at 16:45 ET. Same DB file as equity_snapshots; new table.
        self.agent_pnl_store = AgentPnLStore(db_path=self._snapshot_db_path)
        self.telegram = TelegramAdapter(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )
        self.alerts = AlertManager(self.bus, settings.ntfy_topic, telegram=self.telegram)

        # Scheduler (NYSE timezone for cron triggers)
        # job_defaults: coalesce collapses a backlog of missed fires into one
        # (we never want to replay 5 stale crypto ticks after a hang); the
        # misfire_grace_time lets a job delayed by a busy worker pool still run
        # within 5 min instead of being dropped silently. max_instances stays 1
        # (the default) — with the LLM request timeout now bounding observe(),
        # a single instance can no longer wedge the slot for over an hour.
        self.scheduler = BackgroundScheduler(
            timezone=ET,
            job_defaults={"coalesce": True, "misfire_grace_time": 300},
        )

        # Background thread state
        self._volatility_thread: threading.Thread | None = None
        self._volatility_stop = threading.Event()
        self._dashboard_thread: threading.Thread | None = None
        self._dashboard_server: Any = None  # werkzeug BaseWSGIServer; shut down in stop()
        self._started_at: datetime | None = None
        self._started = False
        self._stopped = False
        self._stop_lock = threading.Lock()
        self._macro_calendar: list[dict[str, Any]] = self._load_macro_calendar()

        # VIX cache: (cached_at, vix_value, vix_bucket).  ^VIX is fetched lazily
        # via market_data; falls back to SWEET_SPOT on failure (warned once).
        self._vix_cache: tuple[datetime, Decimal | None, VixBucket] | None = None
        self._vix_warned: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Boot every subsystem. Idempotent — second call is a no-op."""
        if self._started:
            return

        # 1. Crash-recovery: replay log, reconcile any open orders against broker.
        if self._run_recover_on_start:
            try:
                summary = self.oms.recover()
                log.info(
                    "OMS recovery: replayed=%d recovered=%d abandoned=%d terminal=%d",
                    summary.orders_replayed,
                    summary.orders_recovered,
                    summary.orders_abandoned,
                    summary.orders_already_terminal,
                )
            except Exception:
                log.exception("OMS recovery failed; starting with empty state")

        # 2. Subsystems (order matters: alerts subscribe before anything publishes)
        self.alerts.start()
        self.heartbeat.start()
        self.equity_snapshotter.start()
        self.budget_watcher.start()
        self.reconciler.start()

        # Start the broker's trade-update stream when supported (AlpacaBroker).
        # Real-time fills land in OMS via the registered callback; the
        # reconciler is the polling fallback.
        start_stream = getattr(self.broker, "start_stream", None)
        if callable(start_stream):
            try:
                start_stream()
            except Exception:
                log.exception("broker.start_stream() failed; relying on reconciler polling")

        # 3. Scheduled jobs
        self._register_jobs()
        if not self.scheduler.running:
            self.scheduler.start()

        # One-shot catch-up: the weekly Friday cron misses entirely whenever
        # the process is down at 17:00 ET (the common case under frequent
        # restarts), so fire an overdue reallocation a short delay after boot.
        # Off the critical path; a no-op unless ≥4 weeks have elapsed.
        self.scheduler.add_job(
            self._catch_up_reallocation,
            "date",
            run_date=datetime.now(UTC) + timedelta(seconds=45),
            id="reallocation_catch_up",
            misfire_grace_time=300,
            coalesce=True,
            replace_existing=True,
        )

        # 4. Optional: dashboard thread
        if self._run_dashboard:
            self._start_dashboard_thread()

        # 5. Optional: reactive volatility scanner
        if self._run_volatility_scanner:
            self._start_volatility_scanner()

        self._started = True
        self._started_at = datetime.now(UTC)
        log.info("App: started at %s (universe=%s)", self._started_at, self.universe)

        # Surface the Manager's current sleeve-weight allocation at boot so
        # silent staleness ({} = no reallocation has run yet) is visible in
        # the log instead of inferred from the absence of writes. The first
        # real reallocation fires on the first Friday at least 4 weeks after
        # the previous run (tracked via `last_capital_reallocation` memory).
        from agents.manager_bridge import SLEEVE_WEIGHTS_FILE, read_sleeve_weights
        weights = read_sleeve_weights()
        if weights:
            log.info(
                "manager_bridge: active sleeve weights from %s: %s",
                SLEEVE_WEIGHTS_FILE,
                {str(k).split(".")[-1]: str(v) for k, v in weights.items()},
            )
        else:
            mtime = (
                SLEEVE_WEIGHTS_FILE.stat().st_mtime
                if SLEEVE_WEIGHTS_FILE.exists() else None
            )
            log.warning(
                "manager_bridge: no active sleeve weights (file=%s exists=%s mtime=%s)"
                " — sleeves running at base 1.0× until next 4-week reallocation",
                SLEEVE_WEIGHTS_FILE,
                SLEEVE_WEIGHTS_FILE.exists(),
                datetime.fromtimestamp(mtime, UTC).isoformat() if mtime else "n/a",
            )

    def stop(self) -> None:
        """Shut everything down gracefully and write a shutdown summary."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True

        log.info("App: stopping...")

        # Stop the dashboard server FIRST so no new HTTP requests start
        # hitting the OMS store / lot ledger after we close them below.
        # Without this, requests in flight at shutdown crash with
        # `sqlite3.ProgrammingError: Cannot operate on a closed database`
        # and the non-daemon per-request worker threads block the
        # interpreter from exiting on SIGINT.
        if self._dashboard_server is not None:
            self._safe_call(self._dashboard_server.shutdown)
        if self._dashboard_thread is not None:
            self._dashboard_thread.join(timeout=5)

        # Stop scheduler so no new jobs fire while we tear down.
        self._safe_call(lambda: self.scheduler.shutdown(wait=False))

        # Stop volatility scanner thread
        self._volatility_stop.set()
        if self._volatility_thread is not None:
            self._volatility_thread.join(timeout=5)

        # Stop ops threads
        stop_stream = getattr(self.broker, "stop_stream", None)
        if callable(stop_stream):
            self._safe_call(stop_stream)
        self._safe_call(self.reconciler.stop)
        self._safe_call(self.budget_watcher.stop)
        self._safe_call(self.equity_snapshotter.stop)
        self._safe_call(self.heartbeat.stop)
        self._safe_call(self.alerts.stop)

        # Snapshot agent memories then close
        for memory in self._memories.values():
            self._safe_call(memory.close)

        # Close OMS store
        self._safe_call(self.store.close)
        self._safe_call(self.telegram.close)

        # Write shutdown summary
        self._write_shutdown_summary()

        log.info("App: stopped")

    # ── State construction ──────────────────────────────────────────────────

    def _live_vix(self) -> tuple[Decimal | None, VixBucket]:
        """Return (vix_value, vix_bucket), fetching ^VIX with a 15-minute cache.

        Falls back to (None, SWEET_SPOT) on failure and logs a single warning.
        """
        now = datetime.now(UTC)
        if self._vix_cache is not None:
            cached_at, value, bucket = self._vix_cache
            if (now - cached_at) < timedelta(minutes=15):
                return value, bucket

        try:
            bar = self.market_data.get_latest_bar("^VIX")
        except Exception:
            bar = None
            if not self._vix_warned:
                log.warning("VIX fetch failed; falling back to SWEET_SPOT", exc_info=True)
                self._vix_warned = True

        if bar is None:
            if not self._vix_warned:
                log.warning("VIX bar unavailable; falling back to SWEET_SPOT")
                self._vix_warned = True
            self._vix_cache = (now, None, VixBucket.SWEET_SPOT)
            return None, VixBucket.SWEET_SPOT

        value = bar.close
        bucket = classify_vix(value)
        self._vix_cache = (now, value, bucket)
        return value, bucket

    def build_agent_state(
        self,
        *,
        agent_id: AgentId | None = None,
        symbols: list[str] | None = None,
        ts: datetime | None = None,
    ) -> AgentState:
        """Snapshot the full system view passed to an agent's observe().

        If `agent_id` is provided, `effective_max_gross` is computed using that
        agent's per-sleeve drawdown bucket (from AgentStateTracker) and base
        leverage cap. If omitted, defaults to HAIKU for back-compat with
        callers that don't yet thread an agent_id through.
        """
        ts = ts if ts is not None else self.clock.now()
        symbols = symbols if symbols is not None else self.universe
        emg_agent = agent_id if agent_id is not None else AgentId.HAIKU

        # Fan out the three independent I/O calls so a slow broker leg doesn't
        # serialize the others on the agent hot path. Each future has its own
        # try/except so a single failure degrades to the same fallback as the
        # original sequential code.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="agent-state") as pool:
            bars_fut = pool.submit(
                self.market_data.get_bars_batch,
                list(symbols),
                ts - timedelta(days=_AGENT_STATE_LOOKBACK_DAYS),
                ts,
            )
            positions_fut = pool.submit(self.broker.list_positions)
            account_fut = pool.submit(self.broker.get_account)

            try:
                bars_by_symbol = bars_fut.result(timeout=30)
            except Exception:
                log.warning("get_bars_batch failed", exc_info=True)
                bars_by_symbol = {}
            for sym in symbols:
                bars_by_symbol.setdefault(sym, [])

            try:
                broker_positions = list(positions_fut.result(timeout=10))
            except Exception:
                log.warning("list_positions failed", exc_info=True)
                broker_positions = []

            try:
                account = account_fut.result(timeout=10)
            except Exception:
                log.warning("get_account failed", exc_info=True)
                account = BrokerAccount(
                    cash=Decimal("0"),
                    equity=Decimal("0"),
                    buying_power=Decimal("0"),
                    pattern_day_trader=False,
                    daytrade_count=0,
                )

        # Sleeve-attributed view: each sleeve agent (Haiku/Sonnet/Opus) sees
        # only positions traceable to its own fills via the LotLedger. Without
        # this filter the LLM treats other sleeves' positions as its own and
        # may issue sells against names it never bought. Manager sees the
        # global view (it manages the portfolio across sleeves).
        #
        # Symbol canonicalization is required on both sides of the membership
        # test: broker positions arrive in slashed crypto form (BTC/USD) while
        # the lots ledger now stores canonical (BTCUSD). Without normalization
        # the filter silently drops every crypto position, making the agent
        # see itself as flat in those names — which has previously caused
        # Sonnet to hallucinate ownership from manager regime context.
        if emg_agent == AgentId.MANAGER:
            positions = [
                replace(p, symbol=normalize_symbol(p.symbol)) for p in broker_positions
            ]
        else:
            agent_qty = self.lots.open_qty_by_symbol(emg_agent)
            positions = [
                replace(p, symbol=normalize_symbol(p.symbol))
                for p in broker_positions
                if normalize_symbol(p.symbol) in agent_qty
            ]

        mc = runtime_store.master_capability
        vix_value, vix_bucket = self._live_vix()

        # Per-agent drawdown bucket from the live tracker.  Manager has no
        # sleeve drawdown semantics, so it defaults to NORMAL.
        try:
            dd_bucket = self.tracker.get_state(emg_agent).drawdown_bucket
        except Exception:
            log.warning(
                "tracker.get_state(%s) failed; defaulting to NORMAL drawdown",
                emg_agent, exc_info=True,
            )
            dd_bucket = DrawdownBucket.NORMAL

        emg = effective_max_gross(
            agent_id=emg_agent,
            master_capability=mc,
            vix_bucket=vix_bucket,
            drawdown_bucket=dd_bucket,
        )

        # Pull last-24h news for this agent's relevant universe.
        try:
            news_since = ts - timedelta(hours=24)
            news_items = self.news_store.recent_for_symbols(
                symbols=symbols, since=news_since, limit=80,
                per_symbol_limit=3,
            )
        except Exception:
            log.warning("news_store.recent_for_symbols failed", exc_info=True)
            news_items = []

        # Recent broker rejections for this agent (last 48 h). Used by
        # signal_fingerprint (gate invalidation) and context prompt (LLM awareness).
        try:
            rejection_since = ts - timedelta(hours=48)
            recent_rejections = self.store.recent_rejections_by_agent(
                agent_id=emg_agent.value,
                since=rejection_since,
                n=10,
            )
        except Exception:
            log.warning("recent_rejections_by_agent failed", exc_info=True)
            recent_rejections = []

        # ── Manager-bridge: pull persisted Manager outputs into AgentState ──
        # Without this block, every sleeve agent sees empty manager_* fields
        # regardless of how often the Manager writes regime reads, briefs, or
        # drawdown directives. The Manager's intelligence reaches the sleeves
        # only via these injected strings.
        manager_mem = self._memories.get(AgentId.MANAGER)
        if manager_mem is not None and emg_agent != AgentId.MANAGER:
            mctx = read_manager_context(manager_mem, emg_agent)
        else:
            mctx = {"regime_text": "", "morning_brief": "", "drawdown_directive": "", "critique": ""}

        return AgentState(
            timestamp=ts,
            bars_by_symbol=bars_by_symbol,
            news=news_items,
            positions=positions,
            account=account,
            kill_switch_state=self.kill.state,
            master_capability=mc,
            effective_max_gross=emg,
            vix_value=vix_value,
            manager_regime_text=mctx["regime_text"],
            manager_critique=mctx["critique"],
            manager_morning_brief=mctx["morning_brief"],
            manager_directive=mctx["drawdown_directive"],
            recent_rejections=recent_rejections,
        )

    # ── Agent dispatch ────────────────────────────────────────────────────────

    def dispatch_observation(self, agent: BaseAgent) -> list[Intent]:
        """Build state, call agent.observe(), route approved intents through planner → OMS."""
        if self.kill.state == KillSwitchState.BUDGET_EXHAUSTED and agent.agent_id != AgentId.HAIKU:
            log.info("Skip %s: budget exhausted (Haiku-only mode)", agent.agent_id)
            return []

        state = self.build_agent_state(agent_id=agent.agent_id)
        snapshot = self._build_market_snapshot(state)

        # Keep mark prices current for drawdown bucket computation.  This runs
        # AFTER state construction; the drawdown bucket baked into this tick's
        # effective_max_gross is therefore last tick's, which is the intended
        # behaviour (bucket transitions lag by one tick — acceptable for a
        # bucket that already requires 5 days of recovery to loosen).
        self.tracker.update_on_mark(agent.agent_id, snapshot.current_prices)

        # Skip-when-unchanged: if the agent's signal inputs match the prior
        # tick, no LLM call is made. Trend flips, bucket changes, position
        # changes, and EMG changes all invalidate the fingerprint.
        fp = agent.signal_fingerprint(state)
        if fp is not None and self._last_fingerprint.get(agent.agent_id) == fp:
            log.info("Skip %s: signals unchanged (fp=%s)", agent.agent_id, fp[:80])
            return []

        try:
            intents = agent.observe(state)
        except BudgetExhausted:
            log.warning("BudgetExhausted while %s observed", agent.agent_id)
            return []
        except Exception:
            log.exception("%s.observe() failed", agent.agent_id)
            return []

        if fp is not None:
            self._last_fingerprint[agent.agent_id] = fp

        accepted: list[Intent] = []
        for intent in intents:
            try:
                submitted = self._submit_one_intent(intent, state, snapshot)
            except Exception:
                # Defensive: any unexpected exception in the dispatch path leaves
                # the intent silently NULL in agent memory and breaks calibration
                # statistics. Stamp `dispatch_error` so the intent has a terminal
                # outcome even when something below the OMS layer raises.
                log.exception(
                    "dispatch failed for %s/%s — stamping outcome",
                    intent.agent_id, intent.symbol,
                )
                self.outcome_recorder.record(
                    intent.id, intent.agent_id, "dispatch_error",
                )
                continue
            if submitted:
                accepted.append(intent)

        log.info(
            "%s observed: %d intents, %d submitted",
            agent.agent_id, len(intents), len(accepted),
        )
        return accepted

    def _submit_one_intent(
        self,
        intent: Intent,
        state: AgentState,
        snapshot: MarketSnapshot,
    ) -> bool:
        """Run a single intent through RiskGate → planner → OMS.

        Used by both the daily dispatch loop and the Opus deep-dive workflow.
        Returns True if an order was submitted.
        """
        core_state = self.tracker.get_state(intent.agent_id)
        # Publish intent.submitted for any subscriber that wants to observe
        # the agent → dispatch handoff (analytics, dashboard, telemetry).
        try:
            from core.events import (
                IntentApprovedEvent,
                IntentRejectedEvent,
                IntentSubmittedEvent,
            )
            self.bus.publish(IntentSubmittedEvent(intent=intent))
        except Exception:
            log.warning("failed to publish IntentSubmitted", exc_info=True)
        decision = self._evaluate_with_risk_gate(intent, state, core_state)
        if not decision.allowed:
            log.info(
                "RiskGate vetoed %s/%s: %s",
                intent.agent_id, intent.symbol, decision.veto_reason,
            )
            try:
                self.bus.publish(IntentRejectedEvent(
                    intent_id=intent.id,
                    reason=decision.veto_reason or "risk_gate",
                ))
            except Exception:
                log.warning("failed to publish IntentRejected", exc_info=True)
            self.outcome_recorder.record(
                intent.id, intent.agent_id,
                f"vetoed:{decision.veto_reason or 'risk_gate'}",
            )
            return False
        try:
            self.bus.publish(IntentApprovedEvent(intent_id=intent.id))
        except Exception:
            log.warning("failed to publish IntentApproved", exc_info=True)

        # T2.1: Manager risk_check on high-conviction, large-weight intents.
        # Never skips for budget reasons — falls back to Sonnet (risk_check_lite)
        # after the daily Opus risk_check ceiling is hit. A skipped review on a
        # bad intent costs orders of magnitude more than the saved compute.
        intent = self._maybe_manager_risk_check(intent, state)
        if intent is None:
            return False  # vetoed by Manager

        plan_result = self.planner.plan(intent, core_state, snapshot)
        if isinstance(plan_result, str):
            log.debug(
                "planner: no order for %s/%s (%s)",
                intent.agent_id, intent.symbol, plan_result,
            )
            self.outcome_recorder.record(intent.id, intent.agent_id, plan_result)
            return False
        order = plan_result

        # Account-level guards (planner-rebalance-delta backstops). Both
        # checks are evaluated for adds only (BUY-side orders that increase
        # gross). Trims pass through — the system needs to be able to
        # de-leverage even from an over-cap state.
        account_veto = self._account_level_pre_check(order, snapshot)
        if account_veto is not None:
            log.warning(
                "account guard veto for %s/%s: %s",
                intent.agent_id, intent.symbol, account_veto,
            )
            self.outcome_recorder.record(intent.id, intent.agent_id, account_veto)
            return False

        try:
            self.oms.submit_order(order)
            return True
        except Exception:
            log.exception("OMS.submit_order failed for %s/%s", intent.agent_id, intent.symbol)
            self.outcome_recorder.record(
                intent.id, intent.agent_id, "submit_error",
            )
            return False

    # planner-rebalance-delta: account-level guards -------------------------

    # Absolute account-leverage backstop. Per-sleeve caps sum to ~1.125× at
    # full extension (Haiku 1.5 + Sonnet 1.25 + Opus 1.0 = 3.75× of $30k on
    # $100k account). 1.5× is the hard wall — anything more requires a
    # planner/risk bug to have produced it, and a stop is warranted.
    _ACCOUNT_LEVERAGE_CAP: Decimal = Decimal("1.50")
    # Buying-power utilization cap: refuse orders that would consume more
    # than 90% of remaining buying power. Avoids Alpaca-side rejections and
    # leaves headroom for crypto sleeve activity.
    _BUYING_POWER_UTILIZATION_CAP: Decimal = Decimal("0.90")

    def _account_level_pre_check(
        self, order: Order, snapshot: MarketSnapshot,
    ) -> str | None:
        """Last-mile guards: account-leverage backstop + buying-power check.

        Both apply to BUY orders only — SELL orders reduce leverage and free
        buying power, so they always pass. Returns an outcome-string veto
        reason, or None to allow.
        """
        if order.side != OrderSide.BUY:
            return None
        try:
            account = self.broker.get_account()
        except Exception:
            log.warning("account_pre_check: broker.get_account failed; "
                        "allowing through", exc_info=True)
            return None

        # Compute this order's notional from the latest mark.
        mark = snapshot.current_prices.get(normalize_symbol(order.symbol))
        if mark is None or mark <= Decimal("0"):
            # Without a mark we can't reason about the dollar impact;
            # let it through (risk gate already approved on a per-sleeve basis).
            return None
        intended_notional = order.qty * mark

        # 1. Buying-power pre-check.
        if account.buying_power > Decimal("0"):
            if intended_notional > self._BUYING_POWER_UTILIZATION_CAP * account.buying_power:
                return (
                    f"account_guard:buying_power "
                    f"(intended=${intended_notional:.0f} "
                    f"> {float(self._BUYING_POWER_UTILIZATION_CAP):.0%} "
                    f"of ${account.buying_power:.0f})"
                )
        elif intended_notional > Decimal("0"):
            return "account_guard:buying_power_zero"

        # 2. Account-leverage backstop. Long market value is derived from
        # equity and cash (equity = cash + lmv), so lmv = equity - cash.
        # Holds for both cash-positive and margin-debt states. Verified
        # against Alpaca's reported long_market_value 2026-05-11.
        if account.equity > Decimal("0"):
            current_lmv = account.equity - account.cash
            projected_lmv = current_lmv + intended_notional
            projected_leverage = projected_lmv / account.equity
            if projected_leverage > self._ACCOUNT_LEVERAGE_CAP:
                return (
                    f"account_guard:leverage_cap "
                    f"(projected={float(projected_leverage):.2f}x "
                    f"> {float(self._ACCOUNT_LEVERAGE_CAP):.2f}x)"
                )
        return None

    # T2.1: Manager risk_check call site -----------------------------------

    # High-conviction trigger (≥9) and material sleeve weight (≥8%) are the
    # plan-2c thresholds for warranting a Manager review. Lower-conviction
    # or smaller-weight intents bypass the call to keep the spend bounded.
    _RISK_CHECK_CONVICTION_MIN: int = 9
    _RISK_CHECK_TARGET_WEIGHT_MIN: Decimal = Decimal("0.08")
    # ~$0.015/day budget allows ~1-2 Opus-priced fires/day; subsequent fires
    # in the same UTC day downgrade to Sonnet (risk_check_lite).
    _RISK_CHECK_OPUS_DAILY_CEILING: int = 2

    def _risk_check_opus_count_today(self) -> int:
        """Count today's Opus-priced risk_check calls (excludes Sonnet downgrades)."""
        try:
            return sum(
                1 for e in self.budget.entries()
                if str(e.get("call_type", "")) == "risk_check"
            )
        except Exception:
            log.warning("risk_check counter: BudgetLedger read failed", exc_info=True)
            return 0

    def _maybe_manager_risk_check(
        self, intent: Intent, state: AgentState,
    ) -> Intent | None:
        """Run Manager review if intent meets conviction+size threshold.

        Returns the (possibly resized) intent on approve/downsize, or None on
        veto. Never skips for budget reasons — downgrades the model class
        instead. See agents/prompts/manager_agent.md for the risk_check.json
        schema (decision ∈ {approve, veto, downsize}, downsize_to_weight).
        """
        if (
            intent.conviction < self._RISK_CHECK_CONVICTION_MIN
            or intent.target_weight < self._RISK_CHECK_TARGET_WEIGHT_MIN
        ):
            return intent

        try:
            if self._risk_check_opus_count_today() < self._RISK_CHECK_OPUS_DAILY_CEILING:
                review = self.manager.risk_check(state, intent, ctx=None)
            else:
                log.info(
                    "risk_check.downgrade:sonnet for intent %s "
                    "(daily Opus ceiling %d hit)",
                    intent.id, self._RISK_CHECK_OPUS_DAILY_CEILING,
                )
                review = self.manager.risk_check_lite(state, intent, ctx=None)
        except Exception:
            log.exception(
                "risk_check failed for intent %s; allowing through",
                intent.id,
            )
            return intent

        decision = str(review.get("decision", "")).lower()
        if decision == "veto":
            log.info(
                "Manager VETO on conv=%d weight=%s intent %s/%s: %s",
                intent.conviction, intent.target_weight,
                intent.agent_id, intent.symbol,
                str(review.get("reason", ""))[:200],
            )
            try:
                from core.events import IntentRejectedEvent  # noqa: PLC0415
                self.bus.publish(IntentRejectedEvent(
                    intent_id=intent.id, reason="vetoed:manager_risk_check",
                ))
            except Exception:
                log.warning("failed to publish IntentRejected (veto)", exc_info=True)
            self.outcome_recorder.record(
                intent.id, intent.agent_id, "vetoed:manager_risk_check",
            )
            return None
        if decision == "downsize":
            new_weight_raw = review.get("downsize_to_weight")
            if new_weight_raw is not None:
                try:
                    new_weight = Decimal(str(new_weight_raw))
                except Exception:
                    log.warning(
                        "Manager downsize weight unparseable (%r); allowing original",
                        new_weight_raw,
                    )
                    return intent
                log.info(
                    "Manager DOWNSIZE intent %s/%s: %s -> %s",
                    intent.agent_id, intent.symbol,
                    intent.target_weight, new_weight,
                )
                return replace(intent, target_weight=new_weight)
        return intent

    def _evaluate_with_risk_gate(
        self,
        intent: Intent,
        state: AgentState,
        core_state: CoreAgentState,
    ) -> Any:
        """Run the intent through RiskGate using live per-agent CoreAgentState."""
        return self.risk.check_intent(
            intent=intent,
            agent_state=core_state,
            effective_gross=state.effective_max_gross,
            positions=[],
            ts=state.timestamp,
        )

    def _build_market_snapshot(self, state: AgentState) -> MarketSnapshot:
        """Derive a MarketSnapshot from the current AgentState."""
        current_prices: dict[str, Decimal] = {}
        realized_vol: dict[str, Decimal] = {}

        for sym, bars in state.bars_by_symbol.items():
            if not bars:
                continue
            current_prices[sym] = bars[-1].close

            # Compute annualized 30-day realized vol from daily log-returns.
            # Fall back to the 8% floor if fewer than 2 bars are available.
            returns = [
                (bars[i].close - bars[i - 1].close) / bars[i - 1].close
                for i in range(max(1, len(bars) - 30), len(bars))
                if bars[i - 1].close > Decimal("0")
            ]
            if len(returns) >= 2:
                n = Decimal(len(returns))
                mean: Decimal = sum(returns, Decimal("0")) / n
                variance: Decimal = sum(((r - mean) ** 2 for r in returns), Decimal("0")) / n
                daily_vol = variance.sqrt() if variance > Decimal("0") else Decimal("0")
                ann_vol = daily_vol * Decimal("252").sqrt()
                realized_vol[sym] = max(ann_vol, Decimal("0.08"))

        vix = state.vix_value or Decimal("18")
        vix_bucket = classify_vix(vix)

        return MarketSnapshot(
            current_prices=current_prices,
            realized_vol_30d=realized_vol,
            vix_bucket=vix_bucket,
            timestamp=state.timestamp,
        )

    def _on_fill_received(self, event: Any) -> None:
        """Book the fill into the lot ledger, then update the tracker.

        Order matters: the tracker reads realized P&L from the LotLedger, so
        lot booking must happen first.
        """
        if not isinstance(event, FillReceivedEvent):
            return
        try:
            self.lots.book_fill(event.fill)
        except Exception:
            log.exception("lots.book_fill failed for fill %s", event.fill.id)
        try:
            self.tracker.update_on_fill(event.fill)
        except Exception:
            log.exception("tracker.update_on_fill failed for fill %s", event.fill.id)

    # ── Scheduled jobs ────────────────────────────────────────────────────────

    def _register_jobs(self) -> None:
        """Register all blueprint §2 cron jobs on the scheduler."""
        et = ET
        sched = self.scheduler

        def weekday(hour: int, minute: int) -> CronTrigger:
            return CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=et)

        # Market-hours weekday jobs (mon-fri)
        sched.add_job(
            self._job_haiku_news_scan, weekday(13, 30),
            id=JOB_HAIKU_NEWS_SCAN, replace_existing=True,
        )
        sched.add_job(
            self._job_haiku_close, weekday(15, 55),
            id=JOB_HAIKU_CLOSE, replace_existing=True,
        )
        sched.add_job(
            self._job_sonnet_eod, weekday(16, 30),
            id=JOB_SONNET_EOD, replace_existing=True,
        )
        # Weekly Thursday deep-dive + Friday manager
        sched.add_job(
            self._job_opus_thursday_deepdive,
            CronTrigger(day_of_week="thu", hour=16, minute=30, timezone=et),
            id=JOB_OPUS_THURSDAY_DEEPDIVE, replace_existing=True,
        )
        # Weekly Monday observe(): lets Opus refresh the watchlist and emit
        # starter intents during initiation mode (holdings < TARGET_HOLDINGS).
        # Without this, a flattened Opus has no path back into the market
        # since the Thursday deep-dive requires holdings or a clean watchlist.
        sched.add_job(
            self._job_opus_monday_init,
            CronTrigger(day_of_week="mon", hour=9, minute=35, timezone=et),
            id=JOB_OPUS_MONDAY_INIT, replace_existing=True,
        )
        # Path A: daily book-building until the Opus sleeve is fully invested.
        # Self-guarded inside the job — no-ops once holdings reach target.
        sched.add_job(
            self._job_opus_daily_init,
            weekday(10, 0),
            id=JOB_OPUS_DAILY_INIT, replace_existing=True,
        )
        sched.add_job(
            self._job_manager_reserve_check,
            CronTrigger(day_of_week="mon", hour=9, minute=40, timezone=et),
            id=JOB_MANAGER_RESERVE_CHECK, replace_existing=True,
        )
        sched.add_job(
            self._job_manager_friday,
            CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=et),
            id=JOB_MANAGER_FRIDAY, replace_existing=True,
        )
        # Daily 8:30 ET premarket brief (Mon-Fri). T2.3 / Plan 2c replaces
        # the prior Manager-on-Opus brief with HaikuSynthesizer at ~50× lower
        # cost. Bridges into the three sleeve agents' next observe() via
        # AgentState.manager_morning_brief.
        sched.add_job(
            self._job_haiku_morning_synthesis,
            CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=et),
            id=JOB_HAIKU_MORNING_SYNTHESIS, replace_existing=True,
        )
        # 24/7 Haiku crypto monitor (every 60 min, UTC-anchored — crypto is global)
        sched.add_job(
            self._job_haiku_crypto, CronTrigger(minute=0, timezone="UTC"),
            id=JOB_HAIKU_CRYPTO, replace_existing=True,
        )
        # UTC midnight budget reset
        sched.add_job(
            self._job_budget_reset, CronTrigger(hour=0, minute=0, timezone="UTC"),
            id=JOB_BUDGET_RESET, replace_existing=True,
        )
        # News fetch: every 30 min during US RTH for fresh headlines.
        sched.add_job(
            self._job_news_fetch,
            CronTrigger(day_of_week="mon-fri", hour="9-16", minute="0,30", timezone=et),
            id=JOB_NEWS_FETCH, replace_existing=True,
        )
        # Nightly: full pull (longer lookback) + retention prune.
        sched.add_job(
            self._job_news_nightly,
            CronTrigger(hour=22, minute=0, timezone=et),
            id=JOB_NEWS_NIGHTLY, replace_existing=True,
        )
        # T1.5 / Plan 2c: per-sleeve P&L attribution snapshot, 16:45 ET Mon-Fri
        # (15 minutes after the close, giving Alpaca's daily bars time to
        # settle so unrealized marks reflect today's session).
        sched.add_job(
            self._job_agent_pnl_snapshot, weekday(16, 45),
            id=JOB_AGENT_PNL_SNAPSHOT, replace_existing=True,
        )
        # T2.4 / Plan 2c: Sunday 18:00 ET adversarial critique. Manager
        # reviews each sleeve's top-3 prior-week intents and persists a
        # per-sleeve critique that the agent reads on its next observe().
        sched.add_job(
            self._job_manager_sunday_critique,
            CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=et),
            id=JOB_MANAGER_SUNDAY_CRITIQUE, replace_existing=True,
        )
        # Portfolio snapshot to Telegram: hourly during US RTH Mon-Fri, plus
        # one daily 09:00 ET ping on Sat/Sun to surface weekend crypto drift
        # without flooding off-hours.
        sched.add_job(
            self._job_portfolio_snapshot,
            CronTrigger(day_of_week="mon-fri", hour="9-16", minute=0, timezone=et),
            id=JOB_PORTFOLIO_SNAPSHOT, replace_existing=True,
        )
        sched.add_job(
            self._job_portfolio_snapshot,
            CronTrigger(day_of_week="sat,sun", hour=9, minute=0, timezone=et),
            id=JOB_PORTFOLIO_SNAPSHOT_WEEKEND, replace_existing=True,
        )

    # Sonnet ---------------------------------------------------------------
    def _job_sonnet_eod(self) -> None:         self.dispatch_observation(self.sonnet)

    # Haiku ----------------------------------------------------------------
    def _job_haiku_news_scan(self) -> None:    self.dispatch_observation(self.haiku)
    def _job_haiku_close(self) -> None:        self.dispatch_observation(self.haiku)
    def _job_haiku_crypto(self) -> None:       self.dispatch_observation(self.haiku)

    # Telegram -------------------------------------------------------------
    def _job_portfolio_snapshot(self) -> None:
        """Hourly portfolio status update to Telegram."""
        if not self.telegram.enabled:
            return
        try:
            account = self.broker.get_account()
            positions = list(self.broker.list_positions())
        except Exception:
            log.warning("portfolio_snapshot: broker fetch failed", exc_info=True)
            return

        kill_state = self.kill.state
        status = "LIVE" if kill_state == KillSwitchState.OK else f"HALTED ({kill_state})"
        lines = [
            f"NAV: ${float(account.equity):,.2f}",
            f"Cash: ${float(account.cash):,.2f}",
            f"Status: {status}",
        ]
        if positions:
            lines.append(f"Positions ({len(positions)}):")
            for p in sorted(positions, key=lambda x: x.symbol):
                mark = float(p.current_price)
                qty = float(p.qty)
                lines.append(f"  {p.symbol}: {qty:.4f} @ ${mark:.2f} (${qty*mark:,.2f})")
        else:
            lines.append("Positions: flat")
        try:
            self.telegram.send_portfolio_snapshot("\n".join(lines))
        except Exception:
            log.warning("portfolio_snapshot: telegram send failed", exc_info=True)

    # Opus -----------------------------------------------------------------
    def _job_opus_thursday_deepdive(self) -> None:
        self._opus_deep_dive_rotation(slot=0)

    def _job_opus_monday_init(self) -> None:
        """Weekly Opus.observe() pass — refreshes watchlist and emits starter
        intents when below TARGET_HOLDINGS. Skip-when-unchanged gating in
        dispatch_observation prevents wasted calls once Opus is fully built
        and signals haven't moved.
        """
        self.dispatch_observation(self.opus)

    def _job_opus_daily_init(self) -> None:
        """Daily (Mon-Fri) book-building cycle, ACTIVE ONLY while the Opus
        sleeve is under-invested (open holdings < TARGET_HOLDINGS).

        Opus's normal cadence is twice-weekly (Mon init + Thu deep-dive). At
        ≤2 starter intents per cycle that deploys its ~$30k sleeve over ~3-4
        weeks, leaving ~30% of NAV sitting in cash — a structural drag against
        SPY on up days. This job accelerates *initial* deployment to a few
        days, then self-disables the moment the book reaches target. It does
        NOT make Opus a daily trader: once in management mode the signal
        fingerprint dedupes unchanged ticks, and this guard stops dispatching
        entirely. Remove this job to revert to pure twice-weekly Opus.
        """
        from agents.opus_agent import TARGET_HOLDINGS  # noqa: PLC0415
        held = self.lots.open_qty_by_symbol(AgentId.OPUS)
        n_held = sum(1 for q in held.values() if q > 0)
        if n_held >= TARGET_HOLDINGS:
            log.info(
                "opus_daily_init: skip — book built (%d/%d holdings)",
                n_held, TARGET_HOLDINGS,
            )
            return
        log.info(
            "opus_daily_init: under-invested (%d/%d holdings) — dispatching "
            "book-building cycle", n_held, TARGET_HOLDINGS,
        )
        self.dispatch_observation(self.opus)

    def _opus_deep_dive_rotation(self, *, slot: int) -> None:
        """Pick the slot-th-oldest deep-dive candidate and run on it.

        Candidate pool = Opus's own open lots ∪ Opus's watchlist. Picks the
        symbol with the oldest `last_deep_dive` memo (missing = oldest). Runs
        the deep-dive, then routes the parsed `intent` field through the
        planner so the analysis can actually open or resize a position.
        """
        opus_held = list(self.lots.open_qty_by_symbol(AgentId.OPUS).keys())
        watchlist = self.opus.get_watchlist()
        # Held first (we owe existing theses fresh diligence), then watchlist
        # candidates. Dedupe while preserving order.
        seen: set[str] = set()
        candidates: list[str] = []
        for sym in [*opus_held, *watchlist]:
            sym_u = sym.upper()
            if sym_u not in seen:
                seen.add(sym_u)
                candidates.append(sym_u)

        # Drop any candidate not in PLUMBING_UNIVERSE: the data layer never
        # fetches bars for off-plumbing names, so the deep-dive would burn
        # an LLM call only for the planner to reject the resulting intent
        # with unsized:no_mark. ASML/TSM in the watchlist were doing exactly
        # this for two weeks straight.
        plumbing = {s.upper() for s in PLUMBING_UNIVERSE}
        eligible = [s for s in candidates if s in plumbing]
        dropped = [s for s in candidates if s not in plumbing]
        if dropped:
            log.warning(
                "opus deep_dive: dropping off-plumbing candidates %s "
                "(no mark data available); fix universe or watchlist",
                dropped,
            )
        candidates = eligible

        if not candidates:
            log.info(
                "opus deep_dive: no eligible holdings or watchlist candidates; skipping",
            )
            return

        opus_mem = self._memories[AgentId.OPUS]

        def _last_dive_for(symbol: str) -> str:
            return opus_mem.recall(f"last_deep_dive:{symbol}") or "0000-00-00"

        ordered = sorted(candidates, key=_last_dive_for)
        if slot >= len(ordered):
            return
        self._opus_run_deep_dive(ordered[slot])

    def _opus_run_deep_dive(self, symbol: str) -> None:
        """Run one Opus deep-dive on a specific symbol. Shared by scheduled
        rotation and the off-schedule event-driven trigger (T2.5)."""
        symbol = symbol.upper()
        opus_mem = self._memories[AgentId.OPUS]
        try:
            self.news_fetcher.fetch_filings([symbol])
        except Exception:
            log.warning("doc_pack: fetch_filings failed for %s", symbol, exc_info=True)
        state = self.build_agent_state(agent_id=AgentId.OPUS)
        try:
            doc_pack = build_doc_pack(symbol, self.news_store)
        except Exception:
            log.exception("build_doc_pack failed for %s", symbol)
            doc_pack = ""

        try:
            data = self.opus.deep_dive(state=state, symbol=symbol, doc_pack=doc_pack)
        except BudgetExhausted:
            log.warning("opus deep_dive: budget exhausted; skipping %s", symbol)
            return
        except Exception:
            log.exception("opus deep_dive failed: %s", symbol)
            return

        opus_mem.remember(f"last_deep_dive:{symbol}", state.timestamp.date().isoformat())

        intent = self.opus.extract_deep_dive_intent(state, data, symbol)
        submitted = False
        if intent is not None:
            snapshot = self._build_market_snapshot(state)
            submitted = self._submit_one_intent(intent, state, snapshot)
        log.info(
            "opus deep_dive complete: %s (intent=%s, submitted=%s)",
            symbol,
            intent.action.value if intent else "hold",
            submitted,
        )

    # News -----------------------------------------------------------------
    def _job_news_fetch(self) -> None:
        """Pull recent news for the active universe. Idempotent — dedup by URL.

        Plan 2c T2.2: immediately after the fetch, score any unscored items
        from the last 48h via the Haiku NewsScorer. Crash-safe: if the bot
        died mid-scoring last cycle, the next fetch sweeps up whatever was
        missed (items without scored_at).
        """
        try:
            self.news_fetcher.fetch_for_universe(self.universe, lookback_days=2)
        except Exception:
            log.exception("news fetch failed")
        try:
            cutoff = datetime.now(UTC) - timedelta(hours=48)
            items = self.news_store.unscored_recent(since=cutoff, limit=50)
            if not items:
                log.info("news_scorer: no items met pre-filter criteria today")
            else:
                scored = self.news_scorer.score_batch(items)
                log.info("news_scorer: scored %d/%d items", scored, len(items))
        except Exception:
            log.exception("news_scorer batch failed")

    def _job_news_nightly(self) -> None:
        """Wider lookback + retention prune. Runs once per day."""
        try:
            self.news_fetcher.fetch_for_universe(self.universe, lookback_days=7)
        except Exception:
            log.exception("nightly news fetch failed")
        try:
            cutoff = default_retention_cutoff()
            removed = self.news_store.prune_older_than(cutoff)
            if removed:
                log.info("news retention prune: removed %d items older than %s",
                         removed, cutoff.date())
        except Exception:
            log.exception("news retention prune failed")

    def _job_agent_pnl_snapshot(self) -> None:
        """Daily 16:45 ET per-sleeve P&L attribution snapshot (T1.5).

        Computes realized + unrealized P&L per agent from the lot ledger
        (open lots marked to latest bar close), writes one row per agent
        to `agent_pnl_daily`, and logs a one-line summary. Same-day
        re-runs (e.g. after crash recovery) update in place via the
        (date, agent_id) primary key.
        """
        try:
            breakdowns = compute_daily_pnl(self.lots, self.store, self.market_data)
        except Exception:
            log.exception("agent_pnl_snapshot: compute_daily_pnl failed")
            return
        snap_date = datetime.now(UTC).astimezone(ET).date()
        try:
            self.agent_pnl_store.write_all(snap_date, breakdowns)
        except Exception:
            log.exception("agent_pnl_snapshot: write_all failed")
            return
        for aid, br in breakdowns.items():
            log.info(
                "agent_pnl_snapshot: %s realized=$%s unrealized=$%s "
                "total=$%s open=%d closed=%d",
                aid.value, br.realized, br.unrealized, br.total,
                br.num_open_lots, br.num_closed_lots,
            )

    # Manager --------------------------------------------------------------
    # M1: target dollar value of the Manager's SPY reserve. Mirrors the
    # $10k Manager slice declared in settings.starting_equity's comment
    # ("$30k × 3 sleeves = $90k deployed, $10k Manager reserve"). Held in
    # SPY rather than cash so the reserve earns market beta — the previous
    # behavior (idle cash) was a structural ~25 bps/quarter drag vs the
    # SPY benchmark.
    _MANAGER_RESERVE_TARGET_USD: Decimal = Decimal("10000")

    def _job_manager_reserve_check(self) -> None:
        """Ensure the Manager sleeve holds ~$10k in SPY.

        Idempotent: checks the lot ledger first; submits a market buy only
        when MANAGER has no open SPY. Self-heals on the next Monday after
        any flatten or partial sell. The order goes through the OMS so
        normal fill/event/lot-booking flow applies — the MANAGER agent_id
        ensures the resulting lot is attributed correctly and not visible
        to sleeve agents through the position-filter view.
        """
        try:
            held = self.lots.open_qty_by_symbol(AgentId.MANAGER)
        except Exception:
            log.exception("manager_reserve_check: lot ledger query failed")
            return
        spy_qty = held.get("SPY", Decimal("0"))
        if spy_qty > Decimal("0"):
            log.info(
                "manager_reserve_check: MANAGER already holds %.4f SPY; nothing to do",
                float(spy_qty),
            )
            return

        # Don't double-submit if a previous run's order is still in flight
        # (e.g. accepted but not yet filled when the bot restarted).
        try:
            open_orders = self.oms.list_open_orders()
        except Exception:
            log.exception("manager_reserve_check: oms.list_open_orders failed")
            return
        for o in open_orders:
            if o.agent_id == AgentId.MANAGER and o.symbol == "SPY":
                log.info(
                    "manager_reserve_check: MANAGER SPY order %s already open "
                    "(state=%s); skipping submit", o.id, o.state,
                )
                return

        # Resolve a SPY mark from the latest cached bar. Falls back to
        # broker latest-price if the bar cache is empty.
        now = self.clock.now()
        try:
            bars = self.market_data.get_bars_batch(
                ["SPY"], now - timedelta(days=10), now,
            )
            spy_bars = bars.get("SPY") or []
        except Exception:
            log.warning("manager_reserve_check: get_bars_batch failed", exc_info=True)
            spy_bars = []
        if spy_bars:
            mark = spy_bars[-1].close
        else:
            log.warning("manager_reserve_check: no SPY bars available; skipping")
            return
        if mark <= Decimal("0"):
            log.warning("manager_reserve_check: non-positive SPY mark %s; skipping", mark)
            return

        # Cash check — don't try to buy if the broker can't cover it.
        try:
            account = self.broker.get_account()
        except Exception:
            log.exception("manager_reserve_check: broker.get_account failed")
            return
        if account.cash < self._MANAGER_RESERVE_TARGET_USD:
            log.warning(
                "manager_reserve_check: insufficient cash $%.2f < target $%.2f; "
                "deferring", float(account.cash), float(self._MANAGER_RESERVE_TARGET_USD),
            )
            return

        # Fractional shares rounded to 4 decimals (Alpaca paper accepts this).
        qty = (self._MANAGER_RESERVE_TARGET_USD / mark).quantize(Decimal("0.0001"))
        if qty <= Decimal("0"):
            log.warning("manager_reserve_check: computed qty %s ≤ 0; skipping", qty)
            return

        order = Order(
            id=new_id(),
            intent_id=new_id(),  # synthetic — no Intent originates this order
            agent_id=AgentId.MANAGER,
            symbol="SPY",
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.MARKET,
            order_class=OrderClass.SIMPLE,
            time_in_force=TimeInForce.DAY,
            state=OrderState.PENDING,
            created_at=now,
        )
        log.info(
            "manager_reserve_check: submitting MANAGER SPY BUY qty=%s @ mark $%.2f "
            "(target $%.2f)", qty, float(mark), float(self._MANAGER_RESERVE_TARGET_USD),
        )
        try:
            result = self.oms.submit_order(order)
        except Exception:
            log.exception("manager_reserve_check: oms.submit_order failed")
            return
        log.info(
            "manager_reserve_check: submitted order=%s accepted=%s rejection=%s",
            order.id, result.accepted, result.rejection_reason,
        )

    def _build_manager_ctx(self, state: AgentState):
        """Assemble the full Manager analytics context. Tolerant of partial data."""
        # OBSERVATIONAL — SPYProvider feeds the growth_metrics_v2 block (daily
        # hit-rate vs SPY). Manager v1 prompt does not act on it; we log it
        # now so v2 has historical data when/if we promote it. Lazy import so
        # offline/tests don't pull the dashboard module unless actually used.
        spy_provider = None
        try:
            from dashboard.spy import SPYProvider  # noqa: PLC0415
            if not hasattr(self, "_spy_provider_for_manager"):
                self._spy_provider_for_manager = SPYProvider()
            spy_provider = self._spy_provider_for_manager
        except Exception:
            log.debug("SPYProvider unavailable for manager v2 metrics", exc_info=True)
        try:
            return build_manager_context(
                state_vix=state.vix_value,
                snapshot_db=self._snapshot_db_path,
                tracker=self.tracker,
                lots=self.lots,
                broker=self.broker,
                news_store=self.news_store,
                memories=self._memories,
                spy_provider=spy_provider,
            )
        except Exception:
            log.exception("build_manager_context failed; manager will run blind")
            return None

    def _job_manager_friday(self) -> None:
        state = self.build_agent_state(agent_id=AgentId.MANAGER)

        # Skip-when-unchanged: regime_read + weekly_journal are gated by a
        # macro-input fingerprint (VIX bucket + aggregate equity + per-sleeve
        # drawdown buckets). Capital reallocation below is NOT gated since
        # it has its own 4-week cadence guard. Event-driven Manager calls
        # (risk_check, drawdown_response, etc.) bypass this entirely.
        _, vix_bucket = self._live_vix()
        sleeve_dd: dict[AgentId, DrawdownBucket] = {}
        for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
            try:
                sleeve_dd[aid] = self.tracker.get_state(aid).drawdown_bucket
            except Exception:
                sleeve_dd[aid] = DrawdownBucket.NORMAL
        mgr_fp = compute_manager_fingerprint(
            vix_bucket=vix_bucket,
            aggregate_equity=state.account.equity,
            sleeve_drawdown_buckets=sleeve_dd,
        )
        gated_strategic_call_ran = False

        ctx = self._build_manager_ctx(state)
        manager_mem = self._memories[AgentId.MANAGER]
        prior_regime = manager_mem.recall("last_regime_read") or ""

        if mgr_fp == self._last_manager_fingerprint:
            log.info(
                "manager.friday: skipping regime_read+weekly_journal "
                "(macro inputs unchanged; fp=%s)", mgr_fp[:16],
            )
        else:
            try:
                regime = self.manager.regime_read(state, prior_regime=prior_regime, ctx=ctx)
                if regime:
                    # Persist for next week's prior_regime_read.
                    manager_mem.remember("last_regime_read", json.dumps(regime))
                gated_strategic_call_ran = True
            except Exception:
                log.exception("manager.regime_read failed")
            try:
                journal = self.manager.weekly_journal(state, ctx=ctx)
                if journal:
                    manager_mem.write_journal(state.timestamp.date(), journal)
                    self.telegram.send_weekly_report(journal)
                gated_strategic_call_ran = True
            except Exception:
                log.exception("manager.weekly_journal failed")
            if gated_strategic_call_ran:
                self._last_manager_fingerprint = mgr_fp

        # Capital reallocation runs on its own 4-week elapsed-time cadence,
        # independent of the regime/journal fingerprint gate above. Extracted
        # so the startup catch-up can fire it too (see _catch_up_reallocation).
        self._run_capital_reallocation_if_due(state, ctx, manager_mem)

    def _run_capital_reallocation_if_due(
        self, state: AgentState, ctx: object, manager_mem: Any
    ) -> None:
        """Run the Manager's capital reallocation iff ≥4 weeks since the last.

        Cadence is tracked by the `last_capital_reallocation` memory key rather
        than calendar alignment, so it fires once per ~4-week window from any
        start date. This must NOT depend on the process being alive at the
        Friday-17:00 cron instant: with frequent external restarts the bot is
        almost never up at that exact minute, which is why the reallocation had
        never run. The startup catch-up calls this directly.
        """
        today = state.timestamp.date()
        last_realloc_raw = manager_mem.recall("last_capital_reallocation") or ""
        try:
            last_realloc_date = date.fromisoformat(last_realloc_raw) if last_realloc_raw else None
        except ValueError:
            last_realloc_date = None
        weeks_since = (
            (today - last_realloc_date).days / 7.0
            if last_realloc_date is not None
            else float("inf")
        )
        if weeks_since < 4.0:
            log.info(
                "manager.capital_reallocation: skipping (only %.1f weeks since "
                "last run; need 4.0)", weeks_since,
            )
            return

        log.info(
            "manager.capital_reallocation: firing (weeks_since_last=%.1f, "
            "last=%s)", weeks_since, last_realloc_date,
        )
        try:
            realloc = self.manager.capital_reallocation(state, ctx=ctx)
        except Exception:
            log.exception("manager.capital_reallocation failed")
            realloc = {}
        # Persist the new sleeve weights so sizing.effective_max_gross
        # picks them up on the next dispatch.
        #
        # The manager prompt (reallocation.json) emits DOLLAR allocations,
        # not multipliers:
        #   {"current_allocation": {"haiku": 1000, ...},
        #    "new_allocation":     {"haiku":  950, "sonnet": 1100, ...}, ...}
        # sizing.effective_max_gross expects MULTIPLIERS (1.0 = base). So we
        # derive multiplier = new_allocation / current_allocation per sleeve.
        # We still accept the legacy shapes (`sleeve_weights` map, or a flat
        # {sleeve: multiplier} dict) for backward-compat.
        log.info(
            "manager.capital_reallocation: raw response keys=%s",
            sorted(realloc.keys()) if isinstance(realloc, dict) else type(realloc),
        )
        new_alloc = realloc.get("new_allocation") if isinstance(realloc, dict) else None
        cur_alloc = realloc.get("current_allocation") if isinstance(realloc, dict) else None
        mapped: dict[AgentId, Decimal] = {}
        if isinstance(new_alloc, dict) and new_alloc:
            # Dollar-allocation shape: convert to multipliers.
            for k, v in new_alloc.items():
                try:
                    aid = AgentId(str(k).lower())
                except (ValueError, TypeError):
                    continue
                try:
                    new_d = Decimal(str(v))
                except Exception:
                    continue
                base = None
                if isinstance(cur_alloc, dict) and k in cur_alloc:
                    try:
                        base = Decimal(str(cur_alloc[k]))
                    except Exception:
                        base = None
                if base and base > 0:
                    mult = new_d / base
                else:
                    # No baseline → treat the allocation as already-normalised
                    # weights and rebase to a 1.0 mean across sleeves below.
                    mult = new_d
                mapped[aid] = mult
            # If we had no per-sleeve baseline, rebase so the mean multiplier
            # is 1.0 (preserves relative tilts without inflating gross).
            if not (isinstance(cur_alloc, dict) and cur_alloc):
                vals = list(mapped.values())
                mean = sum(vals) / Decimal(len(vals)) if vals else Decimal(0)
                if mean > 0:
                    mapped = {a: (m / mean) for a, m in mapped.items()}
        else:
            # Legacy shapes: {"sleeve_weights": {...}} or flat {sleeve: mult}.
            weights_raw = (
                realloc.get("sleeve_weights")
                if isinstance(realloc, dict)
                and isinstance(realloc.get("sleeve_weights"), dict)
                else realloc
            )
            for k, v in (weights_raw or {}).items():
                try:
                    aid = AgentId(str(k).lower())
                except (ValueError, TypeError):
                    continue
                try:
                    mapped[aid] = Decimal(str(v))
                except Exception:
                    continue
        # Clamp to the ±25% per-4-week step the prompt promises (defence in
        # depth — Python enforces, the LLM should already respect it).
        lo, hi = Decimal("0.75"), Decimal("1.25")
        mapped = {a: max(lo, min(hi, m)) for a, m in mapped.items()}
        if mapped:
            write_sleeve_weights(mapped)
            log.info("manager.capital_reallocation: persisted sleeve weights %s", mapped)
        else:
            log.warning(
                "manager.capital_reallocation: produced NO usable weights "
                "(new_allocation=%s current_allocation=%s) — sleeves stay at base 1.0x",
                new_alloc, cur_alloc,
            )
        # Stamp the run regardless of whether weights changed, so a "hold
        # everything" decision still satisfies the 4-week cadence.
        manager_mem.remember("last_capital_reallocation", today.isoformat())

    def _catch_up_reallocation(self) -> None:
        """One-shot post-boot catch-up for an overdue capital reallocation.

        The Friday-17:00 cron only fires if the process is alive at that exact
        minute. Frequent external restarts mean it almost never is, so without
        this the 4-week reallocation never runs. Scheduled a short delay after
        boot (off the start() critical path) and a no-op when not yet due.
        """
        try:
            state = self.build_agent_state(agent_id=AgentId.MANAGER)
            ctx = self._build_manager_ctx(state)
            manager_mem = self._memories[AgentId.MANAGER]
            self._run_capital_reallocation_if_due(state, ctx, manager_mem)
        except Exception:
            log.exception("manager.capital_reallocation: startup catch-up failed")

    def _job_manager_sunday_critique(self) -> None:
        """Weekly Sunday 18:00 ET adversarial critique (T2.4 / Plan 2c).

        For each sleeve agent, pulls the top-3 prior-week intents (ranked
        by conviction × target_weight as a proxy for "intents that mattered"
        — see followups for the full P&L-ordered selection) and asks the
        Manager to red-team them. The returned per-intent critiques are
        grouped per sleeve and persisted via write_adversarial_critique,
        so each affected agent sees its critique in `manager_critique` on
        the next observe() cycle.

        The plan handoff specified "3 worst-realized-P&L intents per
        sleeve" but agent_pnl_daily aggregates per (date, agent) — not
        per intent — and the lot ledger discards partial-exit prices,
        so per-intent realized P&L would require walking OMS fill events
        and reconstructing the BUY -> intent linkage. Filed for a future
        Tier 3 enhancement; conviction × target_weight is the heuristic
        used here and it picks the "high-stakes" intents the critique
        prompt is calibrated for.
        """
        state = self.build_agent_state(agent_id=AgentId.MANAGER)
        since = datetime.now(UTC) - timedelta(days=7)
        manager_mem = self._memories[AgentId.MANAGER]

        per_sleeve_picks: dict[AgentId, list[Intent]] = {}
        for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
            rows = self._memories[aid].top_intents_since(since=since, n=3)
            picks: list[Intent] = []
            for r in rows:
                try:
                    picks.append(Intent(
                        id=uuid.UUID(str(r["intent_id"])),
                        agent_id=aid,
                        symbol=str(r["symbol"]),
                        action=Action(str(r["action"])),
                        target_weight=Decimal(str(r["target_weight"])),
                        sleeve=Sleeve.EQUITY,  # placeholder; not used by critique
                        signal="(historical)",  # placeholder
                        conviction=int(r["conviction"] or 0),
                        rationale=str(r["rationale"] or ""),
                        timestamp=datetime.fromisoformat(str(r["logged_at"])),
                    ))
                except Exception:
                    log.warning(
                        "sunday_critique: skipping unreconstructable intent %r",
                        r.get("intent_id"), exc_info=True,
                    )
            per_sleeve_picks[aid] = picks

        all_intents: list[Intent] = [
            i for picks in per_sleeve_picks.values() for i in picks
        ]
        if not all_intents:
            log.info("manager_sunday_critique: no intents in last 7d; skipping")
            return

        try:
            result = self.manager.adversarial_critique(state, all_intents, ctx=None)
        except Exception:
            log.exception("manager.adversarial_critique failed")
            return

        # Group critiques by sleeve and persist a per-sleeve text blob.
        critiques = result.get("critiques") if isinstance(result, dict) else None
        if not isinstance(critiques, list) or not critiques:
            log.info("manager_sunday_critique: empty/invalid critique response")
            return

        by_sleeve: dict[AgentId, list[str]] = {
            AgentId.HAIKU: [], AgentId.SONNET: [], AgentId.OPUS: [],
        }
        for c in critiques:
            if not isinstance(c, dict):
                continue
            try:
                aid = AgentId(str(c.get("agent", "")).lower())
            except ValueError:
                continue
            if aid not in by_sleeve:
                continue
            objection = str(c.get("red_team_objection", "")).strip()
            evidence = str(c.get("what_evidence_would_change_my_mind", "")).strip()
            severity = str(c.get("severity", "minor"))
            summary = str(c.get("summary_of_intent", "")).strip()
            block = (
                f"[{severity}] {summary}\n"
                f"  objection: {objection}\n"
                f"  flip evidence: {evidence}"
            )
            by_sleeve[aid].append(block)

        for aid, blocks in by_sleeve.items():
            if not blocks:
                continue
            critique_text = "\n\n".join(blocks)[:600]
            try:
                write_adversarial_critique(manager_mem, aid, critique_text)
            except Exception:
                log.warning(
                    "sunday_critique: write_adversarial_critique failed for %s",
                    aid, exc_info=True,
                )
        log.info(
            "manager_sunday_critique: wrote critiques for %s",
            sorted(aid.value for aid, blocks in by_sleeve.items() if blocks),
        )

    def _job_haiku_morning_synthesis(self) -> None:
        """Daily 08:30 ET premarket brief (T2.3 / Plan 2c).

        Replaces the prior Manager-on-Opus morning brief. HaikuSynthesizer
        reads four input streams (positions, last-week per-sleeve P&L,
        top-5 high-impact news, VIX bucket) and composes a 180-260 word
        markdown brief that bridges into sleeve agents' next observe()
        via AgentState.manager_morning_brief. ~$0.005 per call vs. the
        prior ~$0.10.
        """
        try:
            positions = positions_from_lot_ledger(self.lots)
            vix_value, vix_bucket = self._live_vix()
            self.haiku_synthesizer.synthesize(
                positions_by_agent=positions,
                vix_value=vix_value,
                vix_bucket=vix_bucket,
            )
        except Exception:
            log.exception("haiku_morning_synthesis failed")

    def _on_news_high_impact(self, event: object) -> None:
        """T2.5: trigger an off-schedule Opus deep dive on a held name when a
        high-impact news item lands. Rate-limited to one extra dive per ISO
        week, tracked via an Opus memory key `extra_dives_iso_week_{YYYY-Www}`.
        """
        symbol = str(getattr(event, "symbol", "") or "").upper()
        if not symbol:
            return
        try:
            held = self.lots.open_qty_by_symbol(AgentId.OPUS)
        except Exception:
            log.warning("on_news_high_impact: lot ledger query failed", exc_info=True)
            return
        # Fire on watchlist names too, not just held. When Opus is flat (as
        # it has been since the 5/11 flatten), restricting to held leaves the
        # event-driven trigger permanently dormant. Watchlist candidates are
        # the names Opus has explicitly flagged for diligence — news on those
        # is exactly the cue to surface them. Must be in PLUMBING_UNIVERSE so
        # the resulting intent has a mark to size against.
        held_syms = {s.upper() for s in held}
        watchlist_syms = {s.upper() for s in self.opus.get_watchlist()}
        plumbing_syms = {s.upper() for s in PLUMBING_UNIVERSE}
        if symbol not in plumbing_syms:
            log.debug(
                "on_news_high_impact: %s not in plumbing universe; ignoring", symbol,
            )
            return
        if symbol not in (held_syms | watchlist_syms):
            log.debug(
                "on_news_high_impact: %s not held or watchlisted by Opus; ignoring",
                symbol,
            )
            return

        # Rate limit: at most one extra dive per ISO week.
        now = datetime.now(UTC)
        iso_year, iso_week, _ = now.isocalendar()
        key = f"extra_dives_iso_week_{iso_year}-W{iso_week:02d}"
        opus_mem = self._memories[AgentId.OPUS]
        if opus_mem.recall(key):
            log.info(
                "on_news_high_impact: rate-limited (already fired extra dive "
                "this ISO week %s); skipping %s", key, symbol,
            )
            return

        log.info(
            "on_news_high_impact: firing off-schedule Opus deep dive on %s "
            "(impact=%s, headline=%s)",
            symbol,
            getattr(event, "impact", "?"),
            str(getattr(event, "headline", ""))[:80],
        )
        try:
            self._opus_run_deep_dive(symbol)
        except Exception:
            log.exception("on_news_high_impact: deep dive failed for %s", symbol)
            return
        # Mark the slot as used only after a successful dive — failed dives
        # don't burn the quota.
        opus_mem.remember(key, symbol)

    def _on_drawdown_ladder_fired(self, event: object) -> None:
        """Wake the Manager when an agent transitions to a worse drawdown bucket.

        Persists a per-sleeve directive that the affected agent reads on its
        next observe() cycle. Cheap ($0.05–$0.10), fires 0–3x/month in normal
        regimes — bounded enough not to overwhelm the budget.
        """
        agent_id = getattr(event, "agent_id", None)
        if agent_id is None or agent_id == AgentId.MANAGER:
            return
        try:
            drawdown_pct = float(getattr(event, "drawdown_pct", 0))
        except Exception:
            drawdown_pct = 0.0
        new_bucket = getattr(event, "new_bucket", "")
        # Only escalate to the Manager for buckets worse than YELLOW. NORMAL
        # transitions (e.g. recovering up from YELLOW) and YELLOW itself are
        # routine and don't warrant a CIO-level response.
        if new_bucket in ("normal", "yellow", ""):
            return
        log.info(
            "manager.drawdown_response triggered: agent=%s bucket=%s dd=%.2%%",
            agent_id, new_bucket, drawdown_pct * 100,
        )
        try:
            state = self.build_agent_state(agent_id=AgentId.MANAGER)
            ctx = self._build_manager_ctx(state)
            # Build attribution dict — best-effort.
            attribution = {str(agent_id).split(".")[-1].lower(): drawdown_pct}
            from agents.manager_bridge import write_drawdown_directive
            from agents.calibration import CalibrationTracker  # noqa: F401
            response = self.manager.drawdown_response(
                state,
                drawdown_pct=drawdown_pct,
                attribution=attribution,
                ctx=ctx,
            )
        except Exception:
            log.exception("manager.drawdown_response failed")
            return
        if not response:
            return
        # The drawdown_response.json schema includes a `directive` (string) and
        # optionally an `actions` list. Surface both to the affected sleeve.
        directive_lines: list[str] = []
        head = response.get("directive") or response.get("summary") or ""
        if isinstance(head, str) and head.strip():
            directive_lines.append(head.strip()[:400])
        actions = response.get("actions")
        if isinstance(actions, list) and actions:
            directive_lines.append("required actions: " + "; ".join(
                str(a)[:80] for a in actions[:5]
            ))
        directive = "\n".join(directive_lines).strip() or json.dumps(response)[:600]
        manager_mem = self._memories[AgentId.MANAGER]
        write_drawdown_directive(manager_mem, agent_id, directive)
        log.info("manager.drawdown_directive written for %s (%d chars)", agent_id, len(directive))

    # Daily housekeeping ---------------------------------------------------
    def _job_budget_reset(self) -> None:
        self.budget.reset_if_new_day(datetime.now(UTC).date())
        self.budget_watcher.reset()
        self.kill.reset_daily()
        log.info("Daily reset: budget + kill switch (UTC midnight)")

    # ── Reactive volatility scanner ──────────────────────────────────────────

    def _start_volatility_scanner(self) -> None:
        if self._volatility_thread is not None and self._volatility_thread.is_alive():
            return
        self._volatility_stop.clear()
        self._volatility_thread = threading.Thread(
            target=self._volatility_loop, daemon=True, name="vol-scanner",
        )
        self._volatility_thread.start()

    def _volatility_loop(self) -> None:
        while not self._volatility_stop.is_set():
            try:
                if self.clock.market_open():
                    self._scan_volatility_once(self.clock.now().date())
            except Exception:
                log.exception("volatility scanner: tick failed")
            self._volatility_stop.wait(300.0)

    def _scan_volatility_once(self, today: date) -> None:
        """Fire on macro event today OR publish PositionIntradayShockEvent on
        >5% intraday move on any held name (T2.5).
        """
        # Macro-event trigger
        macro_today = [
            e for e in self._macro_calendar if str(e.get("date")) == today.isoformat()
        ]
        if macro_today:
            log.info("Macro event today (%d): triggering Haiku scan", len(macro_today))
            self.dispatch_observation(self.haiku)
            return

        # T2.5: per-held-symbol intraday shock detection.
        # Compute current_price / prev_close - 1 for every name any agent holds.
        # Publish PositionIntradayShockEvent on |move| > 5%. Tolerant of
        # missing bars (skip those symbols silently).
        from core.events import PositionIntradayShockEvent  # noqa: PLC0415
        try:
            broker_positions = list(self.broker.list_positions())
        except Exception:
            log.warning("_scan_volatility_once: broker positions unavailable", exc_info=True)
            return
        held_symbols = sorted({normalize_symbol(p.symbol) for p in broker_positions})
        if not held_symbols:
            return

        shock_threshold = Decimal("0.05")
        for symbol in held_symbols:
            try:
                bars = self.market_data.get_bars(
                    symbol,
                    start=datetime.combine(today, datetime.min.time(), tzinfo=UTC)
                    - timedelta(days=5),
                    end=datetime.combine(today, datetime.min.time(), tzinfo=UTC)
                    + timedelta(days=1),
                )
            except Exception:
                continue
            if len(bars) < 2:
                continue
            prev_close = bars[-2].close
            current_price = bars[-1].close
            if prev_close <= Decimal("0"):
                continue
            shock_pct = (current_price - prev_close) / prev_close
            if abs(shock_pct) < shock_threshold:
                continue
            # Find agents holding this symbol so subscribers can target by sleeve.
            holders: list[AgentId] = []
            for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS):
                try:
                    if symbol in self.lots.open_qty_by_symbol(aid):
                        holders.append(aid)
                except Exception:
                    continue
            try:
                self.bus.publish(PositionIntradayShockEvent(
                    symbol=symbol,
                    prev_close=prev_close,
                    current_price=current_price,
                    shock_pct=shock_pct,
                    agent_holders=tuple(holders),
                ))
            except Exception:
                log.warning(
                    "_scan_volatility_once: bus.publish shock for %s failed",
                    symbol, exc_info=True,
                )

    # ── Dashboard thread ─────────────────────────────────────────────────────

    def live_metrics(self) -> "LiveMetrics":
        """Snapshot runtime values for the dashboard top strip.

        Reflects current MC, the live VIX bucket, haiku's drawdown bucket, and
        kill-switch halt state — i.e. exactly what flows into the agents'
        effective_max_gross today.  Haiku is used for emg because it has the
        largest base cap (1.5×); per-agent emg variations are visible via the
        agents' own logs.
        """
        from dashboard.data import LiveMetrics  # noqa: PLC0415

        mc = runtime_store.master_capability
        _, vix_bucket = self._live_vix()
        try:
            dd_bucket = self.tracker.get_state(AgentId.HAIKU).drawdown_bucket
        except Exception:
            dd_bucket = DrawdownBucket.NORMAL
        emg = effective_max_gross(
            agent_id=AgentId.HAIKU,
            master_capability=mc,
            vix_bucket=vix_bucket,
            drawdown_bucket=dd_bucket,
        )
        return LiveMetrics(
            master_capability=mc,
            effective_max_gross=emg,
            vix_bucket=vix_bucket.value if hasattr(vix_bucket, "value") else str(vix_bucket),
            halted=self.kill.state != KillSwitchState.OK,
        )

    def _start_dashboard_thread(self) -> None:
        try:
            from dashboard.data import DashboardData  # noqa: PLC0415
            from dashboard.server import build_app  # noqa: PLC0415
            from dashboard.spy import SPYProvider  # noqa: PLC0415
        except Exception:
            log.warning("dashboard not available; skipping", exc_info=True)
            return

        data = DashboardData(
            oms_store=self.store,
            budget=self.budget,
            calibration=self.calibration,
            memories=dict(self._memories),
            snapshot_db_path=self._snapshot_db_path,
            lots_db_path=Path(self.lots._db_path) if hasattr(self.lots, "_db_path") else None,
            metrics_provider=self.live_metrics,
        )
        flask_app = build_app(data, SPYProvider())

        # Use werkzeug's make_server directly (rather than flask_app.run) so
        # we can hold a reference to the server and call shutdown() cleanly
        # in stop(). flask_app.run() blocks forever and leaves non-daemon
        # per-request handler threads alive, which prevents the interpreter
        # from exiting after the first SIGINT — a user-visible bug where the
        # process kept serving /api/activity (against a closed DB) for
        # minutes after "App: stopped".
        try:
            from werkzeug.serving import make_server  # noqa: PLC0415
        except Exception:
            log.warning("werkzeug.make_server unavailable; skipping dashboard", exc_info=True)
            return

        server = make_server("127.0.0.1", 8081, flask_app, threaded=True)
        # daemon_threads=True ensures Werkzeug's per-request handler threads
        # don't block interpreter exit if a request is mid-flight at shutdown.
        server.daemon_threads = True
        self._dashboard_server = server

        def _run() -> None:
            try:
                server.serve_forever()
            except Exception:
                log.exception("dashboard thread crashed")

        self._dashboard_thread = threading.Thread(target=_run, daemon=True, name="dashboard")
        self._dashboard_thread.start()
        log.info("Dashboard: thread started on http://127.0.0.1:8081")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_broker(self) -> Broker:
        """Select the broker adapter from settings.broker_kind (default: alpaca)."""
        kind = (self.settings.broker_kind or "alpaca").lower()
        if kind == "robinhood":
            return self._build_robinhood_broker()
        if kind == "alpaca":
            return self._build_alpaca_broker()
        raise RuntimeError(f"Unknown broker_kind={kind!r} (expected 'alpaca' or 'robinhood')")

    def _build_robinhood_broker(self) -> Broker:
        """Construct RobinhoodBroker (agentic MCP).

        live_trading_enabled defaults False → dry-run (logs intended orders, sends
        nothing). Real-money guard: refuse to arm live without an auth token.
        """
        from execution.robinhood_broker import RobinhoodBroker  # noqa: PLC0415
        live = bool(self.settings.robinhood_live_enabled)
        if live and not self.settings.robinhood_auth_token:
            raise RuntimeError(
                "robinhood_live_enabled=True but robinhood_auth_token is empty "
                "(real-money guard)"
            )
        if live:
            log.warning(
                "RobinhoodBroker armed for LIVE trading — real money. Ensure the MCP "
                "tool schema has been verified against list_tools() first."
            )
        return RobinhoodBroker(
            auth_token=self.settings.robinhood_auth_token,
            mcp_url=self.settings.robinhood_mcp_url,
            live_trading_enabled=live,
        )

    def _build_alpaca_broker(self) -> Broker:
        """Construct AlpacaBroker (paper). Imported lazily so tests don't need alpaca-py."""
        if not self.settings.alpaca_paper:
            raise RuntimeError("Refusing to start with alpaca_paper=False (real-money guard)")
        try:
            from execution.alpaca_broker import AlpacaBroker  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "AlpacaBroker requires alpaca-py to be installed; pass broker= for tests"
            ) from exc
        return AlpacaBroker(
            api_key=self.settings.alpaca_api_key,
            secret_key=self.settings.alpaca_secret_key,
            paper=self.settings.alpaca_paper,
        )

    def _build_market_data(self) -> MarketData:
        source = (self.settings.market_data_source or "yfinance").lower()
        if source == "alpaca":
            try:
                from data.market import AlpacaMarketData  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "AlpacaMarketData requires alpaca-py; pass market_data= for tests"
                ) from exc
            underlying: MarketData = AlpacaMarketData(
                api_key=self.settings.alpaca_api_key,
                secret_key=self.settings.alpaca_secret_key,
            )
        elif source == "yfinance":
            from data.market import YFinanceMarketData  # noqa: PLC0415
            underlying = YFinanceMarketData(
                alpaca_api_key=self.settings.alpaca_api_key,
                alpaca_secret_key=self.settings.alpaca_secret_key,
            )
        else:
            raise ValueError(f"Unknown market_data_source: {source!r}")

        from data.bar_cache import BarCache, CachedMarketData  # noqa: PLC0415
        cache = BarCache(Path(self.settings.data_dir) / "bars_cache.db")
        return CachedMarketData(underlying, cache)

    def _load_macro_calendar(self) -> list[dict[str, Any]]:
        path = Path(__file__).parent / "config" / "macro_events.yaml"
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text())
            events = data.get("events", []) if isinstance(data, dict) else []
            return list(events) if isinstance(events, list) else []
        except Exception:
            log.exception("Failed to load macro_events.yaml")
            return []

    def _write_shutdown_summary(self) -> None:
        ts = datetime.now(UTC)
        path = self._logs_dir / f"shutdown_{ts.strftime('%Y%m%dT%H%M%SZ')}.json"
        summary = {
            "shutdown_at": ts.isoformat(),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "kill_switch_state": str(self.kill.state),
            "budget_spent_today": str(self.budget.today_spent()),
            "alerts_sent": self.alerts.sent_count,
            "open_orders": len(self.oms.list_open_orders()),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(summary, indent=2))
            log.info("Shutdown summary written: %s", path)
        except Exception:
            log.exception("Failed to write shutdown summary")

    @staticmethod
    def _safe_call(fn: Any) -> None:
        try:
            fn()
        except Exception:
            log.exception("shutdown step failed (continuing)")


# ─── Entrypoint ───────────────────────────────────────────────────────────────


class _RateLimitFilter(logging.Filter):
    """Collapse repeated identical log records to one per interval.

    The Alpaca trading websocket logs a full ERROR + traceback on every
    reconnect attempt; during a DNS/network drop that is thousands of identical
    records per minute, which shreds log rotation and destroys observability.
    This filter lets the FIRST occurrence through (so a real outage is visible),
    suppresses repeats within `min_interval_s`, and annotates the next emitted
    copy with how many were suppressed. Keyed by logger name + message prefix,
    so distinct errors are tracked independently.
    """

    def __init__(self, min_interval_s: float = 60.0) -> None:
        super().__init__()
        self._min = min_interval_s
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        key = f"{record.name}:{str(record.msg)[:80]}"
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is None or (now - last) >= self._min:
                n = self._suppressed.pop(key, 0)
                if n:
                    record.msg = f"{record.msg} [+{n} identical suppressed in last {self._min:.0f}s]"
                    record.args = ()
                self._last[key] = now
                return True
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False


def _setup_logging(level: str) -> None:
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_path = Path(os.environ.get("APP_LOG_FILE", "logs/app.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    lvl = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(lvl)
    # Clear any existing handlers (idempotent across restarts/tests).
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Silence chatty third-party loggers — they drown out our own signal.
    # Werkzeug logs every dashboard poll at INFO; apscheduler announces every
    # job add/start; alpaca's stream pings INFO on each connection event.
    # We still want WARNING+ from all of them so real failures surface.
    # httpx is also added to the silence list: it logs every outbound HTTP
    # request at INFO, which (a) drowns out our own signal and (b) leaks the
    # Telegram bot token in URLs like /bot{TOKEN}/sendMessage. Real failures
    # still surface at WARNING+.
    for noisy in ("werkzeug", "apscheduler", "alpaca", "alpaca.trading.stream", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Rate-limit the Alpaca websocket reconnect storm: it logs an ERROR +
    # traceback on every retry during a network drop (thousands/min), which was
    # rotating 10 MB of logs every ~minute and destroying observability. One
    # record per minute per distinct message keeps the signal, kills the flood.
    # Attached to the stream logger (ERROR-level, so the WARNING cap above does
    # not catch it) and the parent alpaca logger for good measure.
    _ratelimit = _RateLimitFilter(min_interval_s=60.0)
    for noisy in ("alpaca.trading.stream", "alpaca"):
        logging.getLogger(noisy).addFilter(_ratelimit)


def main() -> int:
    settings = Settings()
    _setup_logging(settings.log_level)

    # Real-money guard
    if not settings.alpaca_paper:
        log.error("alpaca_paper=False detected; refusing to start. Set alpaca_paper=True.")
        return 2

    # Master-capability override guard
    if settings.master_capability > Decimal("1.5") and not settings.override_key:
        log.error(
            "master_capability=%s > 1.5 without OVERRIDE_KEY; refusing to start.",
            settings.master_capability,
        )
        return 2

    app = App(settings, run_dashboard=True, run_volatility_scanner=True)

    shutdown_event = threading.Event()

    def _handler(signum: int, _frame: FrameType | None) -> None:
        log.info("Signal %d received; initiating graceful shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    app.start()
    try:
        while not shutdown_event.is_set():
            time.sleep(1.0)
    finally:
        app.stop()
        # Python's concurrent.futures atexit handler joins every ThreadPoolExecutor
        # thread with no timeout.  If an LLM job was in-flight at SIGINT time
        # (Anthropic SDK default timeout = 600 s) the interpreter would wait up
        # to 10 minutes before exiting.  All meaningful cleanup (memory flush,
        # DB close, shutdown summary) has already been done in app.stop() above,
        # so we can hard-exit here and skip the thread-wait entirely.
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
