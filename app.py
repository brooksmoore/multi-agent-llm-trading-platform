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
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.base import AgentState, BaseAgent
from agents.haiku_agent import HaikuAgent
from agents.llm import HAIKU_MODEL, OPUS_MODEL, SONNET_MODEL, BudgetExhausted, LLMClient
from agents.manager_agent import ManagerAgent
from agents.memory import AgentMemory
from agents.opus_agent import OpusAgent
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
from ops.alerts import AlertManager
from ops.heartbeat import HeartbeatWriter

if TYPE_CHECKING:
    from data.market import Bar, MarketData

log = logging.getLogger(__name__)

# ─── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_UNIVERSE: list[str] = [
    "SPY", "QQQ", "IWM", "TQQQ", "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META",
]

# ─── Scheduler job IDs (used by tests and introspection) ─────────────────────

JOB_SONNET_PRE_OPEN          = "sonnet_pre_open"          # 09:25 ET
JOB_SONNET_MID_MORNING       = "sonnet_mid_morning"       # 10:30 ET
JOB_SONNET_MIDDAY            = "sonnet_midday"            # 12:00 ET
JOB_HAIKU_NEWS_SCAN          = "haiku_news_scan"          # 13:30 ET
JOB_SONNET_POWER_HOUR        = "sonnet_power_hour"        # 15:00 ET
JOB_HAIKU_CLOSE              = "haiku_close"              # 15:55 ET
JOB_SONNET_EOD               = "sonnet_eod"               # 16:30 ET
JOB_OPUS_DAILY               = "opus_daily"               # 16:30 ET
JOB_OPUS_THURSDAY_DEEPDIVE   = "opus_thursday_deepdive"   # Thu 16:30 ET
JOB_OPUS_FRIDAY_DEEPDIVE     = "opus_friday_deepdive"     # Fri 16:30 ET
JOB_MANAGER_FRIDAY           = "manager_friday"           # Fri 17:00 ET
JOB_HAIKU_CRYPTO             = "haiku_crypto"             # 24/7, 60-min
JOB_BUDGET_RESET             = "budget_reset"             # UTC midnight

ALL_JOB_IDS: frozenset[str] = frozenset({
    JOB_SONNET_PRE_OPEN, JOB_SONNET_MID_MORNING, JOB_SONNET_MIDDAY,
    JOB_HAIKU_NEWS_SCAN, JOB_SONNET_POWER_HOUR, JOB_HAIKU_CLOSE,
    JOB_SONNET_EOD, JOB_OPUS_DAILY, JOB_OPUS_THURSDAY_DEEPDIVE,
    JOB_OPUS_FRIDAY_DEEPDIVE, JOB_MANAGER_FRIDAY, JOB_HAIKU_CRYPTO,
    JOB_BUDGET_RESET,
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

        # Core singletons -----------------------------------------------------
        self.bus = EventBus()
        self.kill = KillSwitchEngine()
        self.lots = LotLedger()
        self.wash = WashSaleChecker()
        self.risk = RiskGate(self.kill, self.wash, self.lots, event_bus=self.bus)

        self.budget = BudgetLedger(self._budget_path, daily_limit=settings.daily_spend_cap)
        self.budget_watcher = BudgetWatcher(self.budget, self.kill)

        self.broker: Broker = broker if broker is not None else self._build_alpaca_broker()
        self.market_data: MarketData = (
            market_data if market_data is not None else self._build_alpaca_market_data()
        )

        self.store = OMSStore(self._oms_db_path)
        self.oms = OMS(self.broker, self.store, self.bus, clock=self.clock)

        tracker_db = str(data_dir / "agent_tracker.db")
        self.tracker = AgentStateTracker(
            kill_switch=self.kill,
            lot_ledger=self.lots,
            starting_equity=settings.starting_equity,
            db_path=tracker_db,
        )
        self.planner = ExecutionPlanner(self.oms, self.lots, self.bus)
        self.bus.subscribe("fill.received", self._on_fill_received)

        self.reconciler = Reconciler(
            self.oms,
            self.broker,
            self.kill,
            interval_secs=settings.reconciler_interval_secs,
            qty_tolerance=settings.reconciler_qty_tolerance,
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

        # Ops
        self.heartbeat = HeartbeatWriter(self._heartbeat_path, kill=self.kill)
        self.alerts = AlertManager(self.bus, settings.ntfy_topic)

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
        self.budget_watcher.start()
        self.reconciler.start()

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
        self._safe_call(self.reconciler.stop)
        self._safe_call(self.budget_watcher.stop)
        self._safe_call(self.heartbeat.stop)
        self._safe_call(self.alerts.stop)

        # Snapshot agent memories then close
        for memory in self._memories.values():
            self._safe_call(memory.close)

        # Close OMS store
        self._safe_call(self.store.close)

        # Write shutdown summary
        self._write_shutdown_summary()

        log.info("App: stopped")

    # ── State construction ──────────────────────────────────────────────────

    def build_agent_state(
        self,
        *,
        symbols: list[str] | None = None,
        ts: datetime | None = None,
    ) -> AgentState:
        """Snapshot the full system view that all four agents consume."""
        ts = ts if ts is not None else self.clock.now()
        symbols = symbols if symbols is not None else self.universe

        bars_by_symbol: dict[str, list[Bar]] = {}
        for sym in symbols:
            try:
                bars_by_symbol[sym] = list(
                    self.market_data.get_bars(
                        sym, start=ts - timedelta(days=400), end=ts
                    )
                )
            except Exception:
                log.warning("get_bars failed for %s", sym, exc_info=True)
                bars_by_symbol[sym] = []

        try:
            positions = list(self.broker.list_positions())
        except Exception:
            log.warning("list_positions failed", exc_info=True)
            positions = []

        try:
            account = self.broker.get_account()
        except Exception:
            log.warning("get_account failed", exc_info=True)
            account = BrokerAccount(
                cash=Decimal("0"),
                equity=Decimal("0"),
                buying_power=Decimal("0"),
                pattern_day_trader=False,
                daytrade_count=0,
            )

        mc = runtime_store.master_capability
        # effective_max_gross requires a per-agent ID — use HAIKU as the base for
        # the shared snapshot. Each agent re-derives its own gross internally
        # via Sizing if needed. We default to SWEET_SPOT VIX + NORMAL drawdown
        # absent live VIX data; ladder updates kick in once sizing is wired.
        emg = effective_max_gross(
            agent_id=AgentId.HAIKU,
            master_capability=mc,
            vix_bucket=VixBucket.SWEET_SPOT,
            drawdown_bucket=DrawdownBucket.NORMAL,
        )

        return AgentState(
            timestamp=ts,
            bars_by_symbol=bars_by_symbol,
            news=[],
            positions=positions,
            account=account,
            kill_switch_state=self.kill.state,
            master_capability=mc,
            effective_max_gross=emg,
            vix_value=None,
        )

    # ── Agent dispatch ────────────────────────────────────────────────────────

    def dispatch_observation(self, agent: BaseAgent) -> list[Intent]:
        """Build state, call agent.observe(), route approved intents through planner → OMS."""
        if self.kill.state == KillSwitchState.BUDGET_EXHAUSTED and agent.agent_id != AgentId.HAIKU:
            log.info("Skip %s: budget exhausted (Haiku-only mode)", agent.agent_id)
            return []

        state = self.build_agent_state()
        snapshot = self._build_market_snapshot(state)

        # Keep mark prices current for drawdown bucket computation.
        self.tracker.update_on_mark(agent.agent_id, snapshot.current_prices)

        try:
            intents = agent.observe(state)
        except BudgetExhausted:
            log.warning("BudgetExhausted while %s observed", agent.agent_id)
            return []
        except Exception:
            log.exception("%s.observe() failed", agent.agent_id)
            return []

        accepted: list[Intent] = []
        for intent in intents:
            core_state = self.tracker.get_state(intent.agent_id)
            decision = self._evaluate_with_risk_gate(intent, state, core_state)
            if not decision.allowed:
                log.info(
                    "RiskGate vetoed %s/%s: %s",
                    intent.agent_id, intent.symbol, decision.veto_reason,
                )
                continue

            order = self.planner.plan(intent, core_state, snapshot)
            if order is None:
                log.debug(
                    "planner: no order for %s/%s (sub-minimum)",
                    intent.agent_id, intent.symbol,
                )
                continue

            try:
                self.oms.submit_order(order)
                accepted.append(intent)
            except Exception:
                log.exception("OMS.submit_order failed for %s/%s", intent.agent_id, intent.symbol)

        log.info(
            "%s observed: %d intents, %d submitted",
            agent.agent_id, len(intents), len(accepted),
        )
        return accepted

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
        """Forward fill events to AgentStateTracker."""
        if not isinstance(event, FillReceivedEvent):
            return
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
            self._job_sonnet_pre_open, weekday(9, 25),
            id=JOB_SONNET_PRE_OPEN, replace_existing=True,
        )
        sched.add_job(
            self._job_sonnet_mid_morning, weekday(10, 30),
            id=JOB_SONNET_MID_MORNING, replace_existing=True,
        )
        sched.add_job(
            self._job_sonnet_midday, weekday(12, 0),
            id=JOB_SONNET_MIDDAY, replace_existing=True,
        )
        sched.add_job(
            self._job_haiku_news_scan, weekday(13, 30),
            id=JOB_HAIKU_NEWS_SCAN, replace_existing=True,
        )
        sched.add_job(
            self._job_sonnet_power_hour, weekday(15, 0),
            id=JOB_SONNET_POWER_HOUR, replace_existing=True,
        )
        sched.add_job(
            self._job_haiku_close, weekday(15, 55),
            id=JOB_HAIKU_CLOSE, replace_existing=True,
        )
        sched.add_job(
            self._job_sonnet_eod, weekday(16, 30),
            id=JOB_SONNET_EOD, replace_existing=True,
        )
        sched.add_job(
            self._job_opus_daily, weekday(16, 30),
            id=JOB_OPUS_DAILY, replace_existing=True,
        )
        # Weekly (Thu/Fri) deep dives + manager
        sched.add_job(
            self._job_opus_thursday_deepdive,
            CronTrigger(day_of_week="thu", hour=16, minute=30, timezone=et),
            id=JOB_OPUS_THURSDAY_DEEPDIVE, replace_existing=True,
        )
        sched.add_job(
            self._job_opus_friday_deepdive,
            CronTrigger(day_of_week="fri", hour=16, minute=30, timezone=et),
            id=JOB_OPUS_FRIDAY_DEEPDIVE, replace_existing=True,
        )
        sched.add_job(
            self._job_manager_friday,
            CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=et),
            id=JOB_MANAGER_FRIDAY, replace_existing=True,
        )
        # 24/7 Haiku crypto monitor (every 60 min)
        sched.add_job(
            self._job_haiku_crypto, CronTrigger(minute=0, timezone=et),
            id=JOB_HAIKU_CRYPTO, replace_existing=True,
        )
        # UTC midnight budget reset
        sched.add_job(
            self._job_budget_reset, CronTrigger(hour=0, minute=0, timezone="UTC"),
            id=JOB_BUDGET_RESET, replace_existing=True,
        )

    # Sonnet ---------------------------------------------------------------
    def _job_sonnet_pre_open(self) -> None:    self.dispatch_observation(self.sonnet)
    def _job_sonnet_mid_morning(self) -> None: self.dispatch_observation(self.sonnet)
    def _job_sonnet_midday(self) -> None:      self.dispatch_observation(self.sonnet)
    def _job_sonnet_power_hour(self) -> None:  self.dispatch_observation(self.sonnet)
    def _job_sonnet_eod(self) -> None:         self.dispatch_observation(self.sonnet)

    # Haiku ----------------------------------------------------------------
    def _job_haiku_news_scan(self) -> None:    self.dispatch_observation(self.haiku)
    def _job_haiku_close(self) -> None:        self.dispatch_observation(self.haiku)
    def _job_haiku_crypto(self) -> None:       self.dispatch_observation(self.haiku)

    # Opus -----------------------------------------------------------------
    def _job_opus_daily(self) -> None:
        self.dispatch_observation(self.opus)

    def _job_opus_thursday_deepdive(self) -> None:
        self._opus_deep_dive_rotation(slot=0)

    def _job_opus_friday_deepdive(self) -> None:
        self._opus_deep_dive_rotation(slot=1)

    def _opus_deep_dive_rotation(self, *, slot: int) -> None:
        """Pick the slot-th-oldest holding and run a deep-dive on it."""
        positions = []
        try:
            positions = self.broker.list_positions()
        except Exception:
            log.warning("opus deep_dive: list_positions failed", exc_info=True)
        if not positions:
            log.info("opus deep_dive: no holdings; skipping")
            return

        # Sort by last_deep_dive_date (oldest first; missing = oldest).
        opus_mem = self._memories[AgentId.OPUS]
        def _last_dive_for(symbol: str) -> str:
            return opus_mem.recall(f"last_deep_dive:{symbol}") or "0000-00-00"
        ordered = sorted([p.symbol for p in positions], key=_last_dive_for)
        if slot >= len(ordered):
            return
        symbol = ordered[slot]
        state = self.build_agent_state()
        try:
            self.opus.deep_dive(state=state, symbol=symbol, doc_pack="")
            opus_mem.remember(f"last_deep_dive:{symbol}", state.timestamp.date().isoformat())
            log.info("opus deep_dive complete: %s", symbol)
        except BudgetExhausted:
            log.warning("opus deep_dive: budget exhausted; skipping %s", symbol)
        except Exception:
            log.exception("opus deep_dive failed: %s", symbol)

    # Manager --------------------------------------------------------------
    def _job_manager_friday(self) -> None:
        state = self.build_agent_state()
        try:
            self.manager.regime_read(state)
        except Exception:
            log.exception("manager.regime_read failed")
        try:
            journal = self.manager.weekly_journal(state, week_data="(no historical snapshot)")
            if journal:
                self._memories[AgentId.MANAGER].write_journal(state.timestamp.date(), journal)
        except Exception:
            log.exception("manager.weekly_journal failed")

        # Every 4th Friday: capital reallocation
        iso_week = state.timestamp.isocalendar().week
        if iso_week % 4 == 0:
            try:
                self.manager.capital_reallocation(state, four_week_snapshot="(no snapshot)")
            except Exception:
                log.exception("manager.capital_reallocation failed")

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

    def _start_dashboard_thread(self) -> None:
        try:
            from dashboard.app import build_app  # type: ignore[attr-defined]  # noqa: PLC0415
            from dashboard.data import DashboardData  # noqa: PLC0415
        except Exception:
            log.warning("dashboard not available (dash not installed); skipping", exc_info=True)
            return

        data = DashboardData(
            oms_store=self.store,
            budget_ledger=self.budget,
            agent_memories=dict(self._memories),
        )
        dash_app = build_app(data)

        def _run() -> None:
            try:
                dash_app.run(host="127.0.0.1", port=8081, debug=False)
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

    def _build_alpaca_market_data(self) -> MarketData:
        try:
            from data.market import AlpacaMarketData  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "AlpacaMarketData requires alpaca-py; pass market_data= for tests"
            ) from exc
        return AlpacaMarketData(
            api_key=self.settings.alpaca_api_key,
            secret_key=self.settings.alpaca_secret_key,
        )

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
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stderr,
    )


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
