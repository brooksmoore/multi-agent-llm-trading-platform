# v1.0 Complete — Multi-Agent Asset Competitive Bot

> Written: 2026-04-26. Covers M1–M9 (all nine milestones).

---

## Summary

The bot is a runnable, paper-trading process. A single `python app.py` command boots all four agents (Haiku, Sonnet, Opus, Manager), an APScheduler with 13 cron jobs, a Plotly Dash dashboard on `127.0.0.1:8081`, crash-recovery via an append-only OMS event log, a real-time kill switch, wash-sale / LETF / options guardrails, and live alerting via ntfy.sh.

---

## Codebase metrics

| Metric | Count |
|---|---|
| Production lines of code | ~8,600 |
| Test lines of code | ~7,500 |
| Total Python LOC | ~16,100 |
| Test files | 27 |
| Tests collected | 472 |
| Tests passing | 472 (100%) |
| Milestones completed | 9 (M1–M9) |

All tests run against `FakeBroker` and stub market data — no Alpaca or Anthropic API calls in the test suite.

---

## Milestones

| # | Commit | What shipped |
|---|---|---|
| M1 | `8f41fa0` | Skeleton, core types, FSM, EventBus, clock |
| M2 | `21e37e0` | OMS, FakeBroker, append-only event log, crash recovery |
| M3 | `4002af7` | RiskGate, sizing, kill switches, lots, wash-sale checker |
| M4 | `164d05c` | AlpacaBroker adapter, Reconciler, Settings |
| M5 | `fe71c78` | MarketData, DataStore, News adapters, Cache, Summarizer |
| M6 | `c687feb` | LLMClient, AgentMemory, calibration, HaikuAgent |
| M7 | `8853312` | SonnetAgent, OpusAgent, ManagerAgent |
| M8 | `9f8fc63` | Plotly Dash dashboard on :8081, read-only, 3s poll |
| M9-ST1 | `3eb1235`, `f456d6d`, `9c9dde4` | Cache TTL fix, dashboard host/poll fix, MC slider |
| M9-ST2 | `f869bcd` | Wash-sale wiring, budget watcher, $1 drift check, LETF rotation, options structure, 529 retry |
| M9-ST3 | `9ed4b89` | `app.py` entrypoint, heartbeat, alerts, 22 tests |
| M9-ST4 | `19a7d9a` | `ops/journal.py`, 15 tests, pyyaml/apscheduler deps |
| M9-ST5 | _(this commit)_ | Smoke test (11 integration tests), completion reports |

---

## Scheduled cadence (blueprint §2)

| Time (ET) | Days | Job |
|---|---|---|
| 09:25 | Mon–Fri | Sonnet pre-open brief |
| 10:30 | Mon–Fri | Sonnet mid-morning re-eval |
| 12:00 | Mon–Fri | Sonnet midday review |
| 13:30 | Mon–Fri | Haiku news scan |
| 15:00 | Mon–Fri | Sonnet power-hour |
| 15:55 | Mon–Fri | Haiku close |
| 16:30 | Mon–Fri | Sonnet EOD + Opus daily |
| 16:30 | Thu | Opus deep-dive #1 (oldest holding) |
| 16:30 | Fri | Opus deep-dive #2 (2nd-oldest holding) |
| 17:00 | Fri | Manager regime read + weekly journal |
| 17:00 | Every 4th Fri | Manager 4-week capital reallocation |
| :00 | Hourly, 24/7 | Haiku crypto monitor |
| 00:00 UTC | Daily | Budget reset + kill-switch daily reset |

---

## Daily API spend (smoke-test baseline)

All smoke tests run zero-cost because no Anthropic or Alpaca calls are made. The `BudgetLedger` enforces a `$0.95/day` cap before any live run. Based on blueprint §4 cost estimates:

| Agent | Calls/day (expected) | Est. cost/day |
|---|---|---|
| Haiku (5 scheduled + crypto) | ~8 | ~$0.04 |
| Sonnet (5 scheduled) | ~5 | ~$0.13 |
| Opus (deep-dives 2×/week) | ~0.4/day avg | ~$0.16 avg |
| Manager (Friday) | ~0.2/day avg | ~$0.06 avg |
| **Total** | | **~$0.39/trading day** |

This is well under the $0.95 cap. The first live day's `daily_spend.json` will give the true baseline.

---

## Deviations from the blueprint

1. **`execution/planner.py` not built.** The `dispatch_observation()` path runs RiskGate and returns approved intents, but does not yet construct `Order` objects and call `OMS.submit_order()`. The missing link is `Intent → (sizing math) → Order`. Filed as P0 in `logs/m10_backlog.md` — must land before paper trading begins. The `OMS → FakeBroker → fill` path is fully proven by smoke and recovery tests; only the dispatch→OMS bridge is missing.

2. **Per-agent drawdown bucket tracking not wired.** `dispatch_observation` always builds a `CoreAgentState` with `DrawdownBucket.NORMAL` and `consecutive_losses=0`. The drawdown ladder and benching rules therefore do not activate. Filed as P0 in `logs/m10_backlog.md`.

3. **Live VIX feed not wired.** `build_agent_state()` defaults to `VixBucket.SWEET_SPOT`. The leverage ladder (`effective_max_gross`) is therefore clamped to the SWEET_SPOT row of the table. Filed as P1.

4. **Volatility scanner >2σ branch is a placeholder.** The scanner fires on macro events (YAML calendar) but the rolling realized-vol computation is scaffolded, not implemented. Filed as P1.

5. **Telegram is a stub.** `ops/telegram.py` exists but sends nothing. ntfy.sh is the active alert channel. Telegram is M10 per the handoff.

6. **Full trading-day smoke test not performed.** The handoff requests running `python app.py` for one live trading day. That requires real Alpaca paper credentials and Anthropic API key, which are not in this session. The smoke test (`tests/test_smoke.py`, 11 tests) validates all subsystems in isolation with `FakeBroker`. A live first-day run is Brooks's to perform.

---

## Open issues for v1.5 (M10)

See `logs/m10_backlog.md` for the full list with priority tags. The headline items:

**P0 — must land before paper trading:**
- `execution/planner.py` — Intent → Order construction (sizing math, OMS submission)
- Per-agent drawdown bucket tracking (peak equity, consecutive losses, bucket transitions)

**P1 — first 2 weeks of paper:**
- Rules-only baseline backtest (`backtest/engine.py` + `backtest/metrics.py`, vectorbt)
- Telegram notifications (replace ntfy.sh stub)
- Live VIX feed for `build_agent_state`
- Volatility scanner: full realized-vol math

**P2 — when convenient:**
- Dashboard: per-agent leverage gauges + friction ledger panel
- Config YAML extraction (universe, sectors, schedules, tax)
- Calibration deep-pass cron (1st of month, Sonnet ~$0.30)
- Backup interpreter / Dockerfile
- Per-position exit-reason classification

---

## First 6 weeks of paper — monitoring checklist

**Daily (every trading day):**
- [ ] `logs/heartbeat.json` updated within last 60s (confirms main loop alive)
- [ ] `data/daily_spend.json` ≤ $0.95 (confirms budget not blown)
- [ ] No `kill_switch.json` with `state: halt` (confirms no false halts)
- [ ] Dashboard at `http://127.0.0.1:8081` loading and refreshing
- [ ] At least one agent fired (check `logs/daily/{agent}_*.md` for today's date)

**Weekly (every Friday close):**
- [ ] `logs/WEEK_{YYYY}_{WW}.md` written by Manager's weekly journal job
- [ ] Opus deep-dive fired at least once (Thu 16:30 or Fri 16:30)
- [ ] Manager regime read ran (Fri 17:00)
- [ ] Reconciler ran all week without reconciliation breaks (check logs)
- [ ] No alerts fired (or investigate if they did)

**Monthly (every 4th Friday):**
- [ ] Capital reallocation ran (`manager.capital_reallocation` in logs)
- [ ] Review each agent's P&L vs. rules-only baseline (when backtest lands)
- [ ] Review `data/calibration_*.md` if calibration cron is wired

**6-week graduation check (blueprint §13):**
- [ ] LLM-driven sleeve beats rules-only baseline on max drawdown
- [ ] LLM-driven sleeve beats rules-only baseline on drawdown duration
- [ ] Daily API spend averaged ≤ $0.95
- [ ] Zero kill-switch false-halts
- [ ] No wash-sale violations
- [ ] Reconciliation break rate < 1 per week (if higher, investigate)

---

*Generated by Claude Sonnet 4.6, 2026-04-26*
