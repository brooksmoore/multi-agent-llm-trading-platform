"""Main entrypoint — orchestrates all four agents, OMS, broker, scheduler.

Run with: `python app.py` from the project root.

Architecture:
    App
    ├── singletons: EventBus, KillSwitchEngine, OMSStore, OMS, RiskGate,
    │              BudgetLedger, BudgetWatcher, LotLedger, WashSaleChecker,
    │              MarketData, Broker, four LLMClients, four AgentMemory dbs,
    │              HaikuAgent, SonnetAgent, OpusAgent, ManagerAgent
    ├── threads:    Reconciler (60s), HeartbeatWriter (30s),
    │               VolatilityScanner (60s during market hours), dashboard,
    │               BudgetWatcher (30s)
    └── scheduler:  BackgroundScheduler with all blueprint §2 cron jobs

Lifecycle:
    SIGINT / SIGTERM → graceful shutdown → flushes OMS log, snapshots
    memories, writes logs/shutdown_TIMESTAMP.json, exits 0.

Crash recovery:
    OMS.recover() replays the append-only event log on every startup, so
    SIGKILL is safe — no state is lost beyond the in-flight broker call,
    which the reconciler closes on the next 60s tick.
"""

from __future__ import annotations

import json
import logging
import signal
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
    write_morning_brief,
    write_sleeve_weights,
)
from agents.llm import HAIKU_MODEL, OPUS_MODEL, SONNET_MODEL, BudgetExhausted, LLMClient
from agents.manager_agent import ManagerAgent
from agents.memory import AgentMemory
from agents.opus_agent import OpusAgent
from agents.outcome_recorder import OutcomeRecorder
from agents.sonnet_agent import SonnetAgent
from config.runtime_store import runtime_store
from config.settings import Settings
from core.clock import ET, Clock, WallClock
from core.events import EventBus, FillReceivedEvent
from core.types import (
    AgentId,
    DrawdownBucket,
    Intent,
    KillSwitchState,
    MarketSnapshot,
    VixBucket,
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
from ops.alerts import AlertManager
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
JOB_MANAGER_FRIDAY           = "manager_friday"           # Fri 17:00 ET
JOB_MANAGER_MORNING_BRIEF    = "manager_morning_brief"    # Mon-Fri 08:30 ET
JOB_HAIKU_CRYPTO             = "haiku_crypto"             # 24/7, 60-min
JOB_BUDGET_RESET             = "budget_reset"             # UTC midnight
JOB_NEWS_FETCH               = "news_fetch"               # every 30 min during RTH
JOB_NEWS_NIGHTLY             = "news_nightly"             # 22:00 ET full pull + prune
JOB_PORTFOLIO_SNAPSHOT       = "portfolio_snapshot"       # hourly RTH Mon-Fri Telegram
JOB_PORTFOLIO_SNAPSHOT_WEEKEND = "portfolio_snapshot_weekend"  # 09:00 ET Sat/Sun

ALL_JOB_IDS: frozenset[str] = frozenset({
    JOB_HAIKU_NEWS_SCAN, JOB_HAIKU_CLOSE,
    JOB_SONNET_EOD, JOB_OPUS_THURSDAY_DEEPDIVE,
    JOB_MANAGER_FRIDAY, JOB_MANAGER_MORNING_BRIEF, JOB_HAIKU_CRYPTO,
    JOB_BUDGET_RESET, JOB_NEWS_FETCH, JOB_NEWS_NIGHTLY,
    JOB_PORTFOLIO_SNAPSHOT, JOB_PORTFOLIO_SNAPSHOT_WEEKEND,
})


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

        self.broker: Broker = broker if broker is not None else self._build_alpaca_broker()
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
        self.bus.subscribe("fill.received", self._on_fill_received)

        self.reconciler = Reconciler(
            self.oms,
            self.broker,
            self.kill,
            interval_secs=settings.reconciler_interval_secs,
            qty_tolerance=settings.reconciler_qty_tolerance,
            bus=self.bus,
        )

        # LLM clients (one per agent so per-agent budgets/cache patterns stay distinct)
        api_key = settings.anthropic_api_key or None
        self._llm_haiku = LLMClient(self.budget, model=HAIKU_MODEL, api_key=api_key)
        self._llm_sonnet = LLMClient(self.budget, model=SONNET_MODEL, api_key=api_key)
        self._llm_opus = LLMClient(self.budget, model=OPUS_MODEL, api_key=api_key)
        self._llm_manager = LLMClient(self.budget, model=OPUS_MODEL, api_key=api_key)

        # Agent memories (one SQLite db each)
        self._memories = {
            AgentId.HAIKU:   AgentMemory(memory_dir / "haiku.db",   AgentId.HAIKU),
            AgentId.SONNET:  AgentMemory(memory_dir / "sonnet.db",  AgentId.SONNET),
            AgentId.OPUS:    AgentMemory(memory_dir / "opus.db",    AgentId.OPUS),
            AgentId.MANAGER: AgentMemory(memory_dir / "manager.db", AgentId.MANAGER),
        }

        self.haiku = HaikuAgent(self._llm_haiku, self._memories[AgentId.HAIKU])
        self.sonnet = SonnetAgent(self._llm_sonnet, self._memories[AgentId.SONNET])
        self.opus = OpusAgent(self._llm_opus, self._memories[AgentId.OPUS])
        self.manager = ManagerAgent(self._llm_manager, self._memories[AgentId.MANAGER])

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
        )

        # Manager drawdown responder: when AgentStateTracker fires a
        # DrawdownLadderFiredEvent (an agent transitions to a worse bucket),
        # call the Manager's drawdown_response and persist the directive so
        # the affected sleeve sees it on its next observe() cycle.
        self.bus.subscribe("drawdown.ladder_fired", self._on_drawdown_ladder_fired)

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

        # Ops
        self.heartbeat = HeartbeatWriter(self._heartbeat_path, kill=self.kill)
        self.equity_snapshotter = EquitySnapshotter(
            db_path=self._snapshot_db_path,
            agent_state_tracker=self.tracker,
            broker=self.broker,
            lot_ledger=self.lots,
        )
        self.telegram = TelegramAdapter(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )
        self.alerts = AlertManager(self.bus, settings.ntfy_topic, telegram=self.telegram)

        # Scheduler (NYSE timezone for cron triggers)
        self.scheduler = BackgroundScheduler(timezone=ET)

        # Background thread state
        self._volatility_thread: threading.Thread | None = None
        self._volatility_stop = threading.Event()
        self._dashboard_thread: threading.Thread | None = None
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
        # real reallocation only fires every 4th Friday (iso_week % 4 == 0).
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

        # Stop scheduler first so no new jobs fire while we tear down.
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
                list(symbols), ts - timedelta(days=400), ts,
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

        plan_result = self.planner.plan(intent, core_state, snapshot)
        if isinstance(plan_result, str):
            log.debug(
                "planner: no order for %s/%s (%s)",
                intent.agent_id, intent.symbol, plan_result,
            )
            self.outcome_recorder.record(intent.id, intent.agent_id, plan_result)
            return False
        order = plan_result

        try:
            self.oms.submit_order(order)
            return True
        except Exception:
            log.exception("OMS.submit_order failed for %s/%s", intent.agent_id, intent.symbol)
            self.outcome_recorder.record(
                intent.id, intent.agent_id, "submit_error",
            )
            return False

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
        sched.add_job(
            self._job_manager_friday,
            CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=et),
            id=JOB_MANAGER_FRIDAY, replace_existing=True,
        )
        # Daily 8:30 ET premarket brief (Mon-Fri). Bridges Manager macro
        # context into all three sleeve agents' next observe() via
        # AgentState.manager_morning_brief.
        sched.add_job(
            self._job_manager_morning_brief,
            CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=et),
            id=JOB_MANAGER_MORNING_BRIEF, replace_existing=True,
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

        if not candidates:
            log.info(
                "opus deep_dive: no holdings or watchlist candidates; skipping",
            )
            return

        opus_mem = self._memories[AgentId.OPUS]

        def _last_dive_for(symbol: str) -> str:
            return opus_mem.recall(f"last_deep_dive:{symbol}") or "0000-00-00"

        ordered = sorted(candidates, key=_last_dive_for)
        if slot >= len(ordered):
            return
        symbol = ordered[slot]

        # Pull fresh SEC filings before assembling the doc_pack so the deep-dive
        # has up-to-date 8-Ks/10-Qs alongside the 90-day news window.
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
        """Pull recent news for the active universe. Idempotent — dedup by URL."""
        try:
            self.news_fetcher.fetch_for_universe(self.universe, lookback_days=2)
        except Exception:
            log.exception("news fetch failed")

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

    # Manager --------------------------------------------------------------
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
        ctx = self._build_manager_ctx(state)
        manager_mem = self._memories[AgentId.MANAGER]
        prior_regime = manager_mem.recall("last_regime_read") or ""
        try:
            regime = self.manager.regime_read(state, prior_regime=prior_regime, ctx=ctx)
            if regime:
                # Persist for next week's prior_regime_read.
                manager_mem.remember("last_regime_read", json.dumps(regime))
        except Exception:
            log.exception("manager.regime_read failed")
        try:
            journal = self.manager.weekly_journal(state, ctx=ctx)
            if journal:
                manager_mem.write_journal(state.timestamp.date(), journal)
                self.telegram.send_weekly_report(journal)
        except Exception:
            log.exception("manager.weekly_journal failed")

        # Every 4th Friday: capital reallocation
        iso_week = state.timestamp.isocalendar().week
        if iso_week % 4 == 0:
            try:
                realloc = self.manager.capital_reallocation(state, ctx=ctx)
            except Exception:
                log.exception("manager.capital_reallocation failed")
                realloc = {}
            # Persist the new sleeve weights so sizing.effective_max_gross
            # picks them up on the next dispatch. Manager returns a dict like
            # {"sleeve_weights": {"haiku": 1.10, "sonnet": 0.90, ...}} or a
            # flat {"haiku": 1.10, ...}; accept both shapes.
            weights_raw = (
                realloc.get("sleeve_weights")
                if isinstance(realloc.get("sleeve_weights"), dict)
                else realloc
            )
            mapped: dict[AgentId, Decimal] = {}
            for k, v in (weights_raw or {}).items():
                try:
                    aid = AgentId(str(k).lower())
                except (ValueError, TypeError):
                    continue
                try:
                    mapped[aid] = Decimal(str(v))
                except Exception:
                    continue
            if mapped:
                write_sleeve_weights(mapped)
                log.info("manager.capital_reallocation: persisted sleeve weights %s", mapped)

    def _job_manager_morning_brief(self) -> None:
        """Daily 8:30 ET premarket brief.

        Manager runs a regime_read with fresh context, persists the result
        so all three sleeve agents pick it up via `manager_morning_brief`
        on their next observe(). Cheap (~$0.10) and gives sleeves daily
        portfolio-level macro context they otherwise lack.
        """
        state = self.build_agent_state(agent_id=AgentId.MANAGER)
        ctx = self._build_manager_ctx(state)
        manager_mem = self._memories[AgentId.MANAGER]
        prior_regime = manager_mem.recall("last_regime_read") or ""
        try:
            regime = self.manager.regime_read(
                state, prior_regime=prior_regime, ctx=ctx,
            )
        except Exception:
            log.exception("manager.morning_brief regime_read failed")
            return
        if not regime:
            return
        # Persist the JSON regime read for next call's prior_regime, AND
        # extract a human-readable brief that sleeves see verbatim.
        try:
            manager_mem.remember("last_regime_read", json.dumps(regime))
        except Exception:
            log.warning("morning_brief: failed to persist regime_read", exc_info=True)
        # Compose a short brief from the most relevant fields. Falls back
        # to whatever the Manager produced if the schema is unexpected.
        brief_lines: list[str] = []
        for key in ("summary", "narrative", "headline"):
            v = regime.get(key)
            if isinstance(v, str) and v.strip():
                brief_lines.append(v.strip())
                break
        for tag, key in (
            ("regime", "regime"),
            ("vix_view", "vix_view"),
            ("rates", "rates"),
            ("risks", "risks"),
            ("note", "note"),
        ):
            v = regime.get(key)
            if isinstance(v, str) and v.strip():
                brief_lines.append(f"{tag}: {v.strip()[:200]}")
            elif isinstance(v, list) and v:
                brief_lines.append(f"{tag}: {', '.join(str(x) for x in v[:5])[:200]}")
        brief = "\n".join(brief_lines)[:1500] or json.dumps(regime)[:1500]
        write_morning_brief(manager_mem, brief)
        log.info("manager.morning_brief written (%d chars)", len(brief))

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
            self._volatility_stop.wait(60.0)

    def _scan_volatility_once(self, today: date) -> None:
        """Fire a Haiku news-scan if a held name moves >2σ or a macro event lands today."""
        # Macro-event trigger
        macro_today = [
            e for e in self._macro_calendar if str(e.get("date")) == today.isoformat()
        ]
        if macro_today:
            log.info("Macro event today (%d): triggering Haiku scan", len(macro_today))
            self.dispatch_observation(self.haiku)
            return
        # >2σ price-move trigger (placeholder — full vol math lives in execution/sizing)
        # Production: compute 30-day rolling realized vol per held name and compare
        # the current 1-bar return. Skipped on first iteration (insufficient history).

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

        def _run() -> None:
            try:
                flask_app.run(host="127.0.0.1", port=8081, debug=False, threaded=True)
            except Exception:
                log.exception("dashboard thread crashed")

        self._dashboard_thread = threading.Thread(target=_run, daemon=True, name="dashboard")
        self._dashboard_thread.start()
        log.info("Dashboard: thread started on http://127.0.0.1:8081")

    # ── Helpers ──────────────────────────────────────────────────────────────

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
    for noisy in ("werkzeug", "apscheduler", "alpaca", "alpaca.trading.stream"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
