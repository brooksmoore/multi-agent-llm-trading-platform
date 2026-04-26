# M10 Backlog (and beyond)

> **Sanctioned dumping ground for everything M9+ doesn't ship.**
>
> Format: each item has a title, a "why it matters" sentence, rough effort estimate, and priority tag (P0 = before paper-trading begins, P1 = within first 2 weeks of paper, P2 = whenever).
>
> Append freely. Do NOT silently defer items inside `build_journal.md` — that's the pattern that compounded into M9. Use this file instead.
>
> Brooks reviews this before planning M10.

---

## P0 — Should be done before paper-trading begins (or very early)

(Empty at M9 start. M9 is supposed to clear the P0 list. If anything ends up here at M9 close, it's an M9 scope failure that needs Brooks's attention.)

---

## P1 — First 2 weeks of paper trading

### Rules-only baseline backtest (vectorbt)

**Why it matters:** Blueprint §13 graduation criterion #5 requires the LLM-driven sleeve to beat the rules-only baseline on max drawdown and drawdown duration. Without a frozen rules-only baseline, the 6-week graduation evaluation is post-hoc vibes, not evidence. The mathematician lens flagged this in CHANGELOG v0.2.

**Approach:** Build `backtest/engine.py` and `backtest/metrics.py` per blueprint §10. Use vectorbt (already in deps; benchmarked viable on M-series Macs in research file 06). For each agent's mandate (Haiku GTAA, Sonnet multi-factor, Opus GARP-rules-proxy), implement the deterministic version. Run against 2021–2026 historical data. Freeze the parameters and the equity curves as `data/baselines/{haiku,sonnet,opus}_baseline.parquet`.

Effort: 2–3 days. Doesn't have to run live in parallel — can replay LLM trades against the same period historically for the comparison.

### Telegram notifications (replace stub)

**Why it matters:** Brooks said hard yes back in v0.3. ntfy.sh is fine as a starter but Telegram is the persistent channel he'll actually check.

**Approach:** Replace `ops/telegram.py` stub with real Bot API integration (python-telegram-bot or aiogram). Subscribe to the same EventBus channels as `ops/alerts.py`. Token in `.env`, Brooks creates the bot via @BotFather and provides `TELEGRAM_BOT_TOKEN` + his `TELEGRAM_CHAT_ID`. Format messages with markdown, include a "ack" button for HALT events.

Effort: 3–4 hours. Test against Brooks's real bot.

---

## P2 — Whenever (nice-to-have, real value, not blocking paper-trading)

### Dashboard: per-agent leverage gauges + friction ledger panel

**Why it matters:** Blueprint §11 v0.4 spec calls for these. Currently the dashboard shows one global "MAX GROSS" but no per-agent `effective_max_gross` / "Leverage Budget Used" gauges, and no friction ledger.

**Approach:** Add three Dash components to `dashboard/components/`: `leverage_gauges.py` (one mini-gauge per agent showing realized_gross / effective_max_gross), `friction_ledger.py` (cumulative slippage + commissions + simulated borrow as % of NAV, with monthly breakdown), `regime_panel.py` (current VIX bucket, drawdown buckets per sleeve, regime tag from Manager's last regime read).

Effort: 4–6 hours.

### Config YAML extraction

**Why it matters:** Blueprint §10 calls for `config/agents.yaml`, `universe.yaml`, `schedules.yaml`, `tax.yaml`. Currently hardcoded inside agent classes (`_EQUITY_UNIVERSE` in `haiku_agent.py`, etc.). Hardcoded values are fine for v1 but make tuning painful and require code edits to change a sector cap or add a ticker.

**Approach:** Move all hardcoded universe/sector/cap/schedule constants into YAML. Load via `pydantic-settings` or simple `yaml.safe_load`. Validate on startup; log loud error and refuse to start if YAML is malformed.

Effort: 4–5 hours.

### Calibration deep-pass cron

**Why it matters:** Blueprint principles call for monthly Sonnet calibration analysis ($0.30 from headroom). Currently `agents/calibration.py` exists but no scheduled job runs it.

**Approach:** Add APScheduler job in `app.py` for the 1st of each month: pull all conviction-tagged intents from the past 30 days, compute Brier scores per agent and per conviction bucket, write report to `logs/calibration_{YYYY-MM}.md`, surface highlights in Manager's next weekly journal.

Effort: 3 hours.

### Backup interpreter / containerization story

**Why it matters:** The audit couldn't run anything live because the cowork sandbox was Python 3.10 and the project is 3.12. If Brooks ever needs to move this to a different Mac or recover from a corrupted env, current setup has no portable bootstrap.

**Approach:** Add a `Dockerfile` (Python 3.12-slim base, `uv sync`, mount `data/` and `logs/` as volumes). Document `docker compose up` as the alternative startup path in README. Keep `python app.py` as primary; Docker is escape hatch.

Effort: 2–3 hours.

### Optional: paid Finnhub upgrade decision

**Why it matters:** Blueprint §17 flagged that Opus deep-dives are missing earnings call Q&A transcripts (paywalled). Currently mitigated via post-earnings press coverage RSS. After 6 weeks of paper, Brooks should look at Opus's deep-dive memos and decide whether the missing Q&A is materially hurting them.

**Approach:** Not a coding task — a judgment call after data exists. If yes, add Finnhub paid adapter (~2 hours of work) and start budgeting $35/mo.

Effort: 2 hours of code if decision is yes; otherwise 0.

### Per-position exit-reason classification

**Why it matters:** Currently when a position closes, we know the price but not *why* the agent closed it (catalyst hit, stop, factor rank dropped, thesis broken, rebalance). Without this, post-mortem analysis is much weaker.

**Approach:** Add a required `exit_reason` field on every close intent (enum: `catalyst_hit`, `stop_loss`, `time_stop`, `factor_drift`, `thesis_broken`, `rebalance`, `wash_sale_avoidance`, `tax_optimization`, `manager_override`, `kill_switch`). Surface in dashboard trade log and weekly journal.

Effort: 2 hours.

### Crypto-on-weekend monitor instrumentation

**Why it matters:** Haiku owns 24/7 crypto monitoring per blueprint. We should be able to see at a glance how often the crypto sleeve fired LLM calls vs. Python-only signal evaluations on weekends, and what the cost was.

**Approach:** Add a "weekend activity" panel to the dashboard. Cumulative LLM calls + cost breakdown by weekend day.

Effort: 2 hours.

---

## P? — Open questions for Brooks (no effort estimate; need decision first)

### Should the rules-only baseline run live in parallel, or only as historical replay?

The audit recommended historical replay (cheaper, easier, doesn't require duplicate broker plumbing). But live-parallel gives a cleaner A/B with identical fills. Brooks's call.

### `MASTER_CAPABILITY` slider behavior on dashboard

When slider moves at runtime, do we (a) propagate immediately to the next agent call, (b) wait until next scheduled cycle boundary, or (c) require an "Apply" confirmation click? Default in M9 is (a). Ask Brooks if he wants (c) for added safety.

### Heartbeat alert threshold

M9 sets it at "missed for >60s." Mac sleep / Wi-Fi blip / Anthropic 5xx burst could plausibly cause a 60s gap that's not really a system failure. Should the threshold be 5min instead, with a separate "stale heartbeat" warning at 60s?

### Trade-day vs. calendar-day budget reset

Currently `daily_spend.json` resets at UTC midnight. Should it instead reset at US market open (9:30 ET = 14:30 UTC), so a single trading day is one budget cycle? Mostly cosmetic but affects how end-of-day reports are scoped.

---

## Bug bin (small, found post-audit, not yet in any milestone)

(Empty at M9 start. Drop bugs here as you find them rather than fixing silently.)

---

*Last updated: 2026-04-26 (M9 start, pre-populated by Claude in cowork session)*


---

## Added by M9 sub-task 3 (2026-04-26)

### execution/planner.py — Intent → Order construction
**Why it matters:** `app.py.dispatch_observation()` runs intents through RiskGate but cannot submit them — there is no ExecutionPlanner yet. Full smoke-test (sub-task 5) needs this for the end-to-end Intent → RiskGate → OMS → fill → ledger update cycle.
**Effort:** ~3 hours (sizing math from `execution/sizing.py` + Order construction with bracket/stop logic).
**Priority:** P0 — blocking sub-task 5 smoke test.

### data/market.py optional-import refactor
**Why it matters:** `data/market.py` imports `alpaca-py` at module top, which means `agents/base.py` (which imports `Bar`) fails at import time without the SDK installed. Tests must install alpaca-py just to import. Refactor: move alpaca imports inside `AlpacaMarketData` constructor.
**Effort:** ~30 minutes.
**Priority:** P2 — not blocking; just an environment-portability improvement.

### Live VIX feed for build_agent_state
**Why it matters:** `App.build_agent_state` defaults to `VixBucket.SWEET_SPOT` because there is no live VIX poll. The full leverage ladder (`effective_max_gross`) is therefore stuck at 1.0× regardless of regime.
**Effort:** ~1 hour (Alpaca has VIX index data; cache via DataStore).
**Priority:** P1 — first 2 weeks of paper.

### Per-agent drawdown bucket tracking
**Why it matters:** `_evaluate_with_risk_gate` builds a CoreAgentState with `drawdown_bucket=NORMAL` and `consecutive_losses=0` regardless of actual sleeve performance. Full per-agent state requires aggregating fills → realized P&L → bucket classifier. Without it, the drawdown ladder never tightens.
**Effort:** ~4 hours (lots ledger → realized PNL → bucket → state). Touches `agents/base.py` and the per-agent memory layer.
**Priority:** P0 — must land before paper trading begins, otherwise the entire risk ladder is decorative.

### Volatility scanner: full realized-vol math
**Why it matters:** `_scan_volatility_once` only fires on macro events; the >2σ price-move branch is a placeholder. Needs 30-day rolling realized vol per held name + current 1-bar return comparison.
**Effort:** ~2 hours.
**Priority:** P1 — first 2 weeks of paper.

### AgentState rename to disambiguate
**Why it matters:** Two different `AgentState` dataclasses — `agents.base.AgentState` (LLM observation snapshot) vs `core.types.AgentState` (per-agent risk record). app.py works around it with `as CoreAgentState`. Rename one (suggest `ObservationState` for the agents-side; `AgentRiskState` or just leave `core.types.AgentState`) to prevent future bugs.
**Effort:** ~1 hour (mechanical rename).
**Priority:** P2.

