# Claude Code — Milestone 10 Handoff

> Paste this into a **fresh** Claude Code session in `~/Desktop/Multi_Agent_Asset_Competitive_Bot` (do not continue the M9 session — fresh context window is cleaner and cheaper for M10's bounded scope).
>
> **Model + effort plan** (Brooks runs the commands at the indicated points):
> - Start: `/model sonnet` and `/think medium` (or your CC version's equivalent — try `/effort medium` or `/reasoning medium` if `/think` doesn't work; `/help` lists what's available)
> - Sub-tasks 1 and 2 → Sonnet 4.6, Medium effort
> - **Before starting sub-task 3 (rules-only baseline backtest), STOP and tell Brooks to run `/think high`.** Wait for confirmation before proceeding. Sub-task 3 has subtle correctness traps (look-ahead bias, slippage realism, walk-forward CV math) that are silent killers if gotten wrong, and this baseline is what your 6-week graduation evaluation depends on. The extra reasoning is worth it.
>
> Effort level switches preserve full conversation context within the same session — no need to re-summarize.

---

## Context

M9 closed clean (472/472 tests passing, `app.py` boots, smoke tests cover the full subsystem chain). Read these first:

1. `logs/m9_complete.md` — what got built and what surprised the previous session.
2. `logs/v1_complete.md` — current state, deviations, the 6-week monitoring checklist.
3. `logs/m10_backlog.md` — the prioritized backlog. Your work this milestone is exactly the P0 items.
4. `blueprint/00_BLUEPRINT.md` §5 (risk system, Layers 1–6), §16 (leverage), §17 (data sources). These are still your spec.
5. The handoff for M9: `CLAUDE_CODE_M9_HANDOFF.md` — non-negotiable rules from the original handoff still apply.

After reading, summarize back to Brooks in ≤200 words: (a) what M10's three sub-tasks accomplish, (b) the order you'll do them in, (c) any genuine ambiguity needing his input. Then start sub-task 1.

## What this milestone exists for

**The bot does not yet trade.** M9 built every subsystem and wired most of them. Two P0 gaps prevent the agent → broker path from completing: there is no `ExecutionPlanner` to convert intents into orders, and the per-agent drawdown bucket is hardcoded to NORMAL, which means the leverage drawdown ladder never actually tightens. M10 sub-task 1 closes both. Sub-tasks 2 and 3 add the live channels and the baseline that the 6-week graduation gate depends on.

**Do not** start any other M10 backlog work until sub-task 1 is shipped, tested, and merged. The bot must be capable of submitting orders and respecting the drawdown ladder before any further feature work.

## Sub-tasks

### Sub-task 1 — `ExecutionPlanner` + per-agent drawdown bucket tracking (≈6–8 hours)

This is the only thing standing between the bot and paper trading. Both items must ship together because the planner consumes the bucket state.

**1A. `execution/planner.py`** — Intent → Order construction.

Requirements:
- Class `ExecutionPlanner` constructed with `OMS`, `Sizing`, `LotLedger`, `Settings`, and an `EventBus`.
- Method `plan(intent: Intent, agent_state: CoreAgentState, market_snapshot: MarketSnapshot) -> Optional[Order]`.
- Translate `target_weight` → dollar value → share quantity using the existing `execution/sizing.py` math:
  - `effective_max_gross = base_max_gross[agent] × MASTER_CAPABILITY × vix_scalar × dd_scalar`
  - `effective_vol_target = base_vol_target[agent] × MASTER_CAPABILITY`
  - `position_value = vol_targeted_size(target_weight, realized_vol_30d, effective_vol_target)`
  - `position_value = min(position_value, effective_max_gross × agent_equity)`
  - `qty = position_value / current_mark` (fractional supported for non-options)
- For options intents (when MLEG action), construct the multi-leg order spec with `OrderClass.MLEG` and `OptionLegRequest` per leg.
- Return `None` if the resulting order would be < $1 notional or zero quantity (don't waste API calls).
- Emit `IntentSizedEvent` to the bus with the full sizing breakdown for dashboard / journal observability.
- All math in Python; no LLM involvement past the intent.

Update `app.py.dispatch_observation()` to call `ExecutionPlanner.plan()` after `RiskGate.check_intent()` returns approval, then submit the resulting order via `OMS.submit_order()`. The chain becomes: agent.observe() → intents → RiskGate (approve) → ExecutionPlanner (size) → OMS.submit_order → Broker → Fill → LotLedger update → Reconciler verifies.

Tests required (target ~25):
- Unit tests for sizing arithmetic (long, short, options legs, fractional, sub-$1 rejection, hitting cap, vol-target binding, all five drawdown buckets, all five VIX buckets).
- Integration test for full chain: agent emits intent → planner sizes → OMS submits → FakeBroker fills → ledger updated → reconciler confirms.
- Test that MASTER_CAPABILITY runtime change between intents is reflected in next sizing.
- Test that drawdown ladder cuts size correctly at each bucket boundary.
- Test that options orders correctly route to MLEG.

**1B. Per-agent drawdown bucket tracking.**

Requirements:
- New module `execution/agent_state_tracker.py` with class `AgentStateTracker`.
- Maintains per-agent: peak equity (rolling 30-day high), current equity, current drawdown %, current `DrawdownBucket`, consecutive losing trades, last bench end time.
- Equity computed from `LotLedger` realized P&L + open positions marked at last available price.
- `update_on_fill(fill: Fill)` — recompute the agent's state on every fill.
- `update_on_mark(agent: AgentId, mark_prices: dict[Symbol, Decimal])` — recompute on each reconciler tick.
- `get_state(agent: AgentId) -> CoreAgentState` — returns the live `CoreAgentState` with correct `drawdown_bucket` and `consecutive_losses`.
- Recovery rule (blueprint §16.3): bucket can only loosen after 5 consecutive trading days inside the better bucket.
- Hooks into existing `KillSwitchEngine.bench_agent(agent_id)` when `consecutive_losses >= 5`.

Update `app.py.build_agent_state()` to use `AgentStateTracker.get_state()` instead of the hardcoded NORMAL bucket. Persist tracker state to SQLite so it survives restarts; rebuild from `LotLedger` history on cold start.

Tests required (target ~20):
- Unit tests for bucket transitions in both directions (tightening immediately, loosening only after 5 days in better bucket).
- Test consecutive-losing-trades counter resets on a winning trade.
- Test agent gets benched at 5 consecutive losses; un-benches after 24h.
- Test cold-start recovery: kill the process, recompute tracker state from `LotLedger`, verify it matches pre-kill state.
- Test FORCED_CASH bucket triggers on >25% drawdown and requires Manager `mc_proposal` to re-enable (this is the >25% rule from §17.7).

**Commit hygiene:** one commit for 1A, one for 1B, one for the `app.py` integration that wires both. Run `pytest`, `ruff check`, `mypy --strict` before each. After the integration commit, the existing smoke test should still pass; add at least one new smoke test that submits an intent end-to-end through the planner.

### Sub-task 2 — Telegram notifications (≈3–4 hours)

Replace the `ops/telegram.py` stub with real `python-telegram-bot` (v21+) integration.

**Architecture clarification (added during M10 in-flight review): use long-polling, not webhooks.** Brooks's privacy rule prohibits any public-facing inbound endpoint, which rules out the standard webhook pattern. `python-telegram-bot`'s `Application.run_polling()` handles this correctly: the bot polls Telegram outbound for updates, no inbound port required, fully local. This is the documented default for the library.

Requirements:
- Subscribe to the same EventBus channels as `ops/alerts.py`: `HALT`, `RECONCILIATION_BREAK`, `BUDGET_EXHAUSTED`, `LEVERAGE_ROTATION_FLAG`, `DEEP_DIVE_COMPLETE`, plus a new `WEEKLY_JOURNAL_WRITTEN` channel.
- Format messages as Telegram MarkdownV2 (escape special chars).
- HALT events get an inline "Acknowledge" button. Button callback (received via long-polling) clears the kill switch via a callback handler registered with `Application.add_handler(CallbackQueryHandler(...))`. **Only kill-switch HALTs are ackable from Telegram — `RECONCILIATION_BREAK` requires code-side review and is notify-only (no ack button).**
- Run the polling loop in a background thread so it does not block `app.py`'s main scheduler thread.
- Token + chat ID via `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- 1-minute deduplication window to prevent flapping spam (same pattern as `ops/alerts.py`).
- Graceful degradation: if Telegram credentials missing or API unreachable, log warning and continue. Telegram failure must not crash the bot.
- **No FastAPI endpoint added.** The bot is local-only; nothing inbound from the public internet ever.

Tests required (target ~10):
- Unit tests for message formatting (correct MarkdownV2 escaping, length limits, button JSON shape).
- Mock the `httpx` client to verify correct payloads.
- Test deduplication suppresses duplicate events within 1 minute.
- Test missing credentials path logs warning and returns gracefully.

**Note:** Brooks needs to create the bot via @BotFather and provide the token + his chat ID. Document this in the README's "first-time setup" section. Until he does, the module loads but doesn't send.

### Sub-task 3 — Rules-only baseline backtest (≈2–3 days)

> **⏸ STOP HERE. Tell Brooks: "Sub-tasks 1 and 2 are committed. Please run `/think high` before I start sub-task 3 — the baseline backtest has subtle correctness traps (look-ahead bias, slippage realism, walk-forward CV) and this baseline is what the 6-week graduation evaluation depends on. Confirm when ready."**

Build the backtest harness blueprint §13 graduation criterion #5 depends on. Without this, the 6-week graduation evaluation is post-hoc vibes, not evidence.

Requirements:
- `backtest/engine.py` — vectorbt-driven backtest harness. Loads historical OHLCV from `DataStore` (or fetches via `yfinance` if not present, with caching). Runs each agent's deterministic mandate as the rules-only baseline.
- `backtest/metrics.py` — Sharpe, Sortino, max drawdown, drawdown duration, Calmar, profit factor, win rate. All decimal-precision; no float drift.
- Three baseline strategies:
  - **Haiku baseline:** Faber GTAA per blueprint §3 — 10 ETFs, 10-month SMA filter, equal-vol-weighted within in-trend assets. Monthly rebalance.
  - **Sonnet baseline:** Multi-factor (value Z + momentum Z + quality Z, equal weights) on liquid US large/mid caps. 10–15 names, monthly rebalance.
  - **Opus baseline:** GARP-rules-proxy (PEG ≤ 1.5, ROE ≥ 15%, top decile by 12-1 momentum within filter). 5–8 names, monthly rebalance.
- Backtest period: 2021-01-01 to 2026-04-25.
- Output: per-strategy equity curve as parquet at `data/baselines/{haiku,sonnet,opus}_baseline.parquet`. Per-strategy metrics as JSON at `data/baselines/{haiku,sonnet,opus}_metrics.json`. Combined report at `data/baselines/baseline_report.md`.
- CLI entry: `python -m backtest.engine --start 2021-01-01 --end 2026-04-25 --strategy all` (or `--strategy haiku|sonnet|opus`).
- Walk-forward CV: 24 monthly windows minimum; report deflated Sharpe per Bailey & López de Prado.
- Survivorship caveat documented in the report (using current S&P 500 constituents; discount returns 1–2%/yr per blueprint §7).

Tests required (target ~20):
- Each strategy: deterministic output for a known input window (lock the equity curve hash for one canonical period).
- Metrics: known-input → known-output unit tests (e.g., constant-return series → known Sharpe).
- Walk-forward: 24 windows produce 24 sub-results.
- CLI integration test that runs `--strategy haiku --start 2024-01-01 --end 2024-06-30` end-to-end and verifies the parquet file exists.
- Cost-realism: backtest must apply slippage at far-touch + 1bp + impact (per blueprint §7). Test that slippage is non-zero.

**Output for Brooks:** at sub-task 3 commit, also write `data/baselines/baseline_report.md` summarizing the three strategies' historical Sharpe / Sortino / max DD / Calmar so he can see what the LLM sleeves need to beat to "graduate."

## Where to put things you can't or shouldn't do in M10

`logs/m10_backlog.md` — same usage rules as M9. P2 items already there: dashboard leverage gauges + friction ledger panel, config YAML extraction, calibration deep-pass cron, Dockerfile, exit-reason classification, weekend crypto monitor panel, the AgentState rename. Do not silently defer items inside `build_journal.md`.

## Process discipline

- One git commit per logical chunk (sub-task 1A, 1B, integration; sub-task 2; sub-task 3 strategies separately, then metrics, then CLI).
- Run `pytest`, `ruff check`, `mypy --strict` before each commit.
- After each sub-task, append to `logs/build_journal.md` with what was built, what surprised you, what's pending.
- If you hit ambiguity that genuinely blocks you, **stop and ask Brooks**. The pattern of silent deferrals is what made M9 necessary.
- If you discover a bug in M1–M9 code while working on M10 that isn't trivially small, **add it to the bug bin in `logs/m10_backlog.md`** rather than fixing silently. Brooks decides priority.

## Things you should NOT do

- Do not enable real-money trading anywhere. Paper only.
- Do not add public-facing endpoints (Telegram outbound to api.telegram.org is fine; no inbound webhook listener that binds to anything other than `127.0.0.1`).
- Do not change the leverage system math (§16) or the prompts.
- Do not run `python app.py` against live Alpaca / live Anthropic in this session — that's Brooks's first-day smoke test, not yours.
- Do not work past sub-task 1 without confirming the planner + drawdown bucket integration works end-to-end against `FakeBroker` and the existing smoke test still passes.

## When you're done

Write `logs/m10_complete.md` with the same structure as `logs/m9_complete.md`:
- What got done (per sub-task, with commits)
- What surprised you
- What's open for M11

Update `logs/v1_complete.md` to v1.1 — the bot can now actually submit orders. Update the deviations list (planner missing → ✅; drawdown bucket → ✅; Telegram → ✅; baseline → ✅).

Then **stop**. Do not begin paper trading on Brooks's behalf. The first live `python app.py` run is his.

## Now

Read the four numbered docs above, summarize the M10 plan back to Brooks in ≤200 words, then start sub-task 1A (`execution/planner.py`).
