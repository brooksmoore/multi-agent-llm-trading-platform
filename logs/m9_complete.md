# M9 Complete

> 2026-04-26. Written by Claude Sonnet 4.6 at milestone close.

---

## What got done

M9 closed the gap between M1–M8's infrastructure and a runnable bot. Five sub-tasks, five commits.

**ST1 (3 commits):** Four blueprint violations fixed — Anthropic cache TTL made explicit (`ttl: "1h"`), dashboard host changed from `0.0.0.0` to `127.0.0.1`, poll interval corrected to 3 s, and the MC label replaced with a Dash slider + runtime settings store (clamped to 1.5 server-side).

**ST2 (1 commit):** Six deferred plumbing items wired — wash-sale check in `RiskGate.check_intent()`, dollar-mismatch tolerance in `Reconciler`, `BudgetWatcher` class that trips `BUDGET_EXHAUSTED` on `BudgetLedger`, LETF anti-rotation rule (21-day window, 3-reopen threshold, `LeverageRotationFlagEvent`), options structural check (naked long/short rejected; vertical spreads, condors, covered calls, CSPs allowed), and exponential backoff for Anthropic 529 errors.

**ST3 (1 commit):** `app.py` built from scratch — 570+ LOC, 13 scheduled jobs with NYSE-timezone cron, crash recovery on startup, graceful shutdown with JSON summary, SIGINT/SIGTERM handlers, real-money guard, Haiku-only degradation on `BUDGET_EXHAUSTED`, reactive volatility scanner (macro-event trigger), deep-dive rotation (oldest holding by `last_deep_dive_date`), and the `ops/heartbeat.py` + `ops/alerts.py` subsystems. 22 tests.

**ST4 (1 commit):** `ops/journal.py` — two atomic-write helpers: `write_weekly_journal()` (ISO-week filename) and `write_daily_memo()` (agent-scoped, date-keyed). Both idempotent. Added `pyyaml` and `apscheduler` to `pyproject.toml`. 15 tests.

**ST5 (this commit):** Smoke test (`tests/test_smoke.py`, 11 tests) validates the full stack in isolation — startup/shutdown, Intent→RiskGate cycle, OMS→FakeBroker fill, heartbeat write, reconciler clean pass, budget watcher no-trip, multi-agent dispatch, journal write, real-money guard, macro calendar load. Completion reports written.

**Final state:** 472 tests, 100% passing. Ruff clean on all changed files.

---

## What surprised me

**The M5–M8 integration debt was worse than it looked.** The audit said "four bugs," but the real problem was that every subsystem was built in isolation and never composed. ST3 was effectively six hours of discovery work — finding that `effective_max_gross` had a different signature than assumed, that two classes named `AgentState` existed in different modules, that `pyyaml` and `apscheduler` weren't installed in the test venv, that `ReconcileSummary` had different field names than the code referenced. None of these were hard to fix; all of them would have been invisible until first run.

**`ExecutionPlanner` is the only meaningful gap.** Everything else in the risk→OMS→broker chain is proven. The missing piece is `Intent → (sleeve equity × target_weight × MC scalar) → qty → Order`. That's maybe 3 hours of work. Without it, agents can observe and emit intents, RiskGate can approve them, but no orders actually get submitted. This is P0 for paper trading.

**The "reactive scans" scope expanded during ST3.** The blueprint described a simple >2σ monitor; building it correctly requires 30-day rolling realized vol per held name, which requires DataStore integration, which is more than the 2 hours allotted. The macro-event branch (YAML calendar) is fully implemented; the price-move branch is an acknowledged placeholder. Filed as P1 in M10 backlog.

**Two `AgentState` classes will bite someone eventually.** `agents.base.AgentState` (LLM observation snapshot passed to `agent.observe()`) and `core.types.AgentState` (per-agent risk record consumed by `RiskGate`) have identical names in different modules. `app.py` works around it with `as CoreAgentState`. This is a rename away from a subtle import bug. Filed as P2 in M10 backlog.

---

## What's open for M10

Full list is in `logs/m10_backlog.md`. The two blockers for paper trading:

1. **`execution/planner.py`** — `Intent → Order` (P0). Without it, no trades execute.
2. **Per-agent drawdown bucket tracking** (P0). Without it, the drawdown ladder never tightens.

Everything else (Telegram, backtest harness, VIX feed, vol math, dashboard gauges, config YAML) is P1 or P2 and doesn't block first paper-trade.

---

## To start paper trading

1. Confirm M10 P0 items are built and tested.
2. Set real Alpaca paper credentials in `.env` (never live keys).
3. Set `ANTHROPIC_API_KEY` in `.env`.
4. Set `NTFY_TOPIC` in `.env` for alerts.
5. Run `python app.py` during market hours.
6. Watch `logs/heartbeat.json`, `data/daily_spend.json`, and the dashboard at `http://127.0.0.1:8081`.
7. After 6 weeks, run the graduation check from `logs/v1_complete.md`.

---

*M9 closed. The bot runs. Paper trading awaits M10 P0 items.*
