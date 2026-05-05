# Claude Code — Milestone 9 Handoff

> Paste this into a fresh Claude Code session in `~/Desktop/Multi_Agent_Asset_Competitive_Bot`.
>
> **Model plan** (Brooks runs `/model <name>` in the same session at the indicated points):
> - Start session with **Sonnet 4.6** (`/model sonnet`)
> - Sub-tasks 1, 2, 4 → Sonnet 4.6
> - **Before starting sub-task 3 (`app.py`), STOP and tell Brooks to run `/model opus`.** Wait for confirmation before proceeding. Sub-task 3 is orchestration/lifecycle/recovery work where one ordering bug bites for weeks; Opus 4.7's deeper reasoning is worth the tokens for the ~400 LOC of code that everything else hangs from.
> - **Before starting sub-task 5 (smoke test + final report), STOP and tell Brooks to run `/model sonnet` again.** Smoke testing and report writing don't need Opus.
>
> Conversation context carries over across `/model` switches in the same session — no need to re-summarize the build state to yourself when the model changes.

---

## Context

You (or a previous Claude Code session) just shipped M1–M8 of this project. The build journal is at `logs/build_journal.md`; an external auditor's review is at `logs/m1_m8_audit.md`. Read both before doing anything else.

**The headline:** the deterministic infrastructure is excellent, but the system is currently a kit, not a bot. M5–M8 each deferred final integration "to the next milestone." M8 ran out of milestones. There is no `app.py` and four small bugs accumulated. M9 closes that gap.

**Do not add scope.** Telegram, the backtest harness, config YAML extraction, per-agent dashboard leverage gauges, and the friction ledger panel are all M10. M9 is exclusively about making M1–M8's existing work actually run end-to-end as one process.

## Read these in order

1. `logs/m1_m8_audit.md` — the full audit. Treat the "Dangerous deviations found" section as your worklist for sub-tasks 1 and 2 below.
2. `logs/build_journal.md` — every "deferred to next milestone" note tells you what's pending.
3. `blueprint/00_BLUEPRINT.md` §2 (cadence), §5 (risk system), §11 (dashboard), §16 (leverage), §17 (data sources). These are your spec.
4. `CLAUDE_CODE_HANDOFF.md` — original handoff. Non-negotiable rules section still applies in full.

After reading, summarize back to me in ≤200 words: (a) what M9 must accomplish, (b) the order you'll do the sub-tasks in, (c) any genuine ambiguity that needs Brooks's input before you start. Do this BEFORE touching a file.

## Sub-tasks

### Sub-task 1 — Fix the four blueprint-violating bugs (≈1 hour)

1. **`agents/llm.py`** — every `cache_control` block must be `{"type": "ephemeral", "ttl": "1h"}` explicitly. Add a unit test that inspects the SDK call args and asserts the TTL is present on every cached message. Without this test, the bug can silently regress.
2. **`dashboard/app.py`** — change `host="0.0.0.0"` to `host="127.0.0.1"` and remove the `# noqa: S104` suppression.
3. **`dashboard/app.py`** — change `POLL_INTERVAL_MS = 5000` to `3000` to match blueprint §11.
4. **`dashboard/app.py` + `dashboard/layout.py`** — replace the static `_strip_cell` for `MASTER_CAPABILITY` with a `dcc.Slider` (range 0.0–1.5, step 0.05, default = `settings.master_capability`). Add a Dash callback that writes the new value to a runtime settings store readable by `execution/sizing.py` between calls. **Note:** the slider must NOT exceed 1.5 unless `OVERRIDE_KEY` env var is set — enforce this server-side, not client-side. Above 1.5, log a warning and clamp to 1.5.

Commit each fix separately so the diff history is reviewable.

### Sub-task 2 — Wire the deferred plumbing (≈3 hours)

1. **Activate wash-sale check in `risk.check_intent()`** — `WashSaleChecker` is already a `__init__` dependency on `RiskGate`. Call `self._wash.is_blocked(symbol)` for every intent that closes a losing position; reject with reason `"wash_sale_window"` and a clear message including the days remaining and suggested proxy from the swap-list. Tests: extend `test_risk_gate.py` with at least 3 wash-sale rejection scenarios and 1 wash-sale allowed-via-proxy scenario.
2. **Add dollar-mismatch to `execution/reconciler.py`** — current code checks share drift only. Add `abs(broker_dollar_value - local_dollar_value) > 1.00` per position. Either trigger halts via `KillSwitchEngine.trip_reconciliation_break()`. Update `test_reconciler.py` to cover dollar-only drift.
3. **Wire `BudgetLedger.is_exhausted()` to `KillSwitchEngine.trip_budget_exhausted()`** — this happens in the orchestration layer (sub-task 3). Add a `BudgetWatcher` class in `execution/budget.py` that polls the ledger and trips the kill switch on exhaustion. Test that a forced exhaustion correctly trips and degrades the system to Haiku-only mode (per blueprint §5 Layer 3).
4. **LETF anti-rotation rule** — `risk.py` should track `(symbol, exit_date)` for LETF closes; flag for Manager review (don't reject, just log + emit event) any reopen of an effectively-equivalent LETF (e.g., TQQQ → UPRO → TQQQ counts as 2 reopens of long-NDX-3x exposure) within 15 trading days. Maintain an "equivalent exposure" map in `risk.py`: `{NDX_LONG_3X: [TQQQ, UPRO], NDX_SHORT_3X: [SQQQ, SPXU], SOX_LONG_3X: [SOXL], TLT_LONG_3X: [TMF], ...}`. Test: 3 reopens within 15 days emits a `LeverageRotationFlag` event.
5. **Defined-risk options structural check** — when `risk.py` sees an MLEG order, verify it matches one of: long debit vertical, short credit vertical, iron condor/butterfly, covered call (against existing equity position of ≥100 shares), cash-secured put (with cash blocked separately). Reject naked anything including naked long calls/puts. Test coverage required.
6. **529 retry with exponential backoff + jitter** in `agents/llm.py` — match the existing `RateLimitError` (429) retry pattern. Cap at 3 retries with backoff `[1, 4, 16]` seconds + 0–1s jitter. Test by monkeypatching the SDK to raise `APIStatusError(529)` and asserting retry count + total elapsed time.

### Sub-task 3 — Build `app.py` (≈6–8 hours)

> **⏸ STOP HERE. Tell Brooks: "Sub-tasks 1 and 2 are committed. Please run `/model opus` before I start sub-task 3 — this is the orchestration/lifecycle work and Opus 4.7's deeper reasoning is the right tool. Confirm when ready."**

This is the heart of M9. The audit calls it the missing entrypoint.

Requirements (derived from blueprint §2 + §5 + §11):

**Lifecycle:**
- Single Python process started with `python app.py` from a regular Terminal window.
- Graceful shutdown on SIGINT/SIGTERM: cancel pending Dash callbacks, flush OMS event log, snapshot agent memory, write a `logs/shutdown_TIMESTAMP.json` summary, exit with status 0.
- On crash: the existing append-only OMS event log + `oms_store.py` recovery handles it. `app.py` re-runs reconciliation on startup.

**What `app.py` owns and starts:**
1. **Settings load** — `pydantic-settings` reads `.env`, sets `MASTER_CAPABILITY`, broker credentials, ntfy topic, Anthropic key.
2. **Singletons** — one each: `OMS`, `RiskGate`, `BudgetLedger`, `BudgetWatcher`, `KillSwitchEngine`, `Reconciler`, `MarketData` (Alpaca websocket), `LotLedger`, `WashSaleChecker`, `EventBus`, `MemoryStore`.
3. **Agents** — instantiate Haiku, Sonnet, Opus, Manager with the singletons injected.
4. **Scheduler (`APScheduler`):**
   - Market-hours gate (9:30–16:00 ET, NYSE calendar from `core/clock.py`):
     - Sonnet pre-open brief at 09:25
     - Sonnet mid-morning re-eval at 10:30
     - Sonnet midday review at 12:00
     - Haiku news scan at 13:30
     - Sonnet power-hour at 15:00
     - Haiku close-of-day at 15:55
     - Sonnet EOD review at 16:30 via Batch API
     - Opus daily prior-memo cached read at 16:30 (cheap)
   - **Thursday 16:30 ET:** Opus deep-dive #1 (rotates through holdings — see "deep-dive scheduler" below)
   - **Friday 16:30 ET:** Opus deep-dive #2
   - **Friday 17:00 ET:** Manager regime read + adversarial critique + weekly journal
   - **Friday of every 4th week:** Manager 4-week capital reallocation
   - **24/7:** Haiku crypto signal monitoring (cheap — only fires LLM call when signal flips)
   - **Reactive (any time):** Volatility-spike trigger — see "reactive scans" below
5. **Reconciliation thread** — runs `Reconciler.tick()` every 60s.
6. **Heartbeat writer** — `ops/heartbeat.py` (build it; see sub-task 4) writes a tick to `logs/heartbeat` every 30s; KillSwitchEngine alerts if missed for >60s.
7. **Budget reset** — at UTC midnight, `BudgetLedger.reset_for_today()`.
8. **Dashboard subprocess** — start `dashboard/app.py` as a separate thread (not subprocess; same SQLite/DuckDB connection pool). Bound to `127.0.0.1:8081`.
9. **EventBus subscribers** — `ops/alerts.py` subscribes to HALT, RECONCILIATION_BREAK, BUDGET_EXHAUSTED, LEVERAGE_ROTATION_FLAG, DEEP_DIVE_COMPLETE; pushes to ntfy.

**Deep-dive scheduler (Opus Thu/Fri):**
- Maintain a rotation queue of Opus's current holdings.
- Thursday picks the holding with the oldest `last_deep_dive_date`. Friday picks the second-oldest.
- Each deep-dive call uses up to ~$0.40 budget (per blueprint §4) — enforced by the `agents/llm.py` budget gate.
- After a deep-dive, persist `last_deep_dive_date` for that symbol in `agents/memory.py`.

**Reactive scans (volatility triggers):**
- Background thread polls `MarketData` every 60s during market hours.
- Triggers a Haiku news-scan call ($0.02) on:
  - Any held name with a >2σ price move on the 30-day rolling realized vol
  - SPY or VIX move >1.5σ
  - Tagged macro events from a small calendar in `config/macro_events.yaml` (FOMC, CPI, NFP, GDP releases — build the YAML with the next 90 days)
- Haiku scan can escalate to Sonnet ($0.06) if it flags the event as "material."
- All reactive calls go through the same budget gate.

**Things `app.py` MUST NOT do:**
- No real-money credentials. `alpaca_paper=True` always.
- No public binding. Dashboard `127.0.0.1:8081`.
- No `MASTER_CAPABILITY > 1.5` without `OVERRIDE_KEY`.
- No bypass of `RiskGate` or `Sizing` for any code path.
- No silent retries on Anthropic 5xx beyond the new exponential backoff.

**Tests:**
- `tests/test_app_lifecycle.py` — start, ingest one fake market data tick, verify all four agents see it, shut down cleanly. Use FakeBroker.
- `tests/test_app_scheduler.py` — assert all scheduled jobs are registered with the right cron expressions.
- `tests/test_app_recovery.py` — start, kill mid-trade (simulate SIGKILL by zeroing out the OMS in-memory state then restarting), verify reconciliation rebuilds correct state from the event log.

### Sub-task 4 — Build the missing ops modules (≈2 hours)

1. **`ops/heartbeat.py`** — single `HeartbeatWriter` class with `start()`/`stop()` and a 30s tick loop. Writes `{"ts": iso8601, "uptime_s": int}` to `logs/heartbeat.json` atomically.
2. **`ops/alerts.py`** — ntfy.sh adapter. Subscribes to EventBus channels (HALT, RECONCILIATION_BREAK, BUDGET_EXHAUSTED, LEVERAGE_ROTATION_FLAG, DEEP_DIVE_COMPLETE). Sends formatted notifications via HTTP POST to `https://ntfy.sh/{NTFY_TOPIC}`. Respect a 1-minute deduplication window so a flapping condition doesn't spam.
3. **`ops/journal.py`** — Manager's weekly journal output (markdown string) gets persisted to `logs/WEEK_{YYYY}_{WW}.md`. Daily agent memos get persisted to `logs/daily/{agent}_{YYYY-MM-DD}.md`. Idempotent on re-run (overwrites the day's file, doesn't append).

Telegram stays a stub. M10.

### Sub-task 5 — Smoke test + final report (≈1 trading day elapsed)

> **⏸ STOP HERE. Tell Brooks: "Sub-tasks 3 and 4 are committed. Please run `/model sonnet` before I start sub-task 5 — smoke testing and report writing don't need Opus. Confirm when ready."**

1. Run `python app.py` for one full trading day with the user-supplied Alpaca paper credentials and Anthropic key.
2. At end of day, verify:
   - Daily Anthropic spend (per `daily_spend.json`) ≤ $0.95
   - All scheduled agent calls fired
   - At least one full agent decision → RiskGate → OMS → FakeBroker (or Alpaca paper) → fill → ledger update cycle completed
   - Reconciler ran every 60s without false halts
   - Heartbeat file updated continuously
   - Dashboard accessible at `http://127.0.0.1:8081` and updating every 3s
   - No real-money paths exercised
3. Write `logs/v1_complete.md` per the original handoff:
   - Total LOC, test coverage %, milestones completed (M1–M9 now)
   - Daily API spend observed vs. budgeted
   - Any deviations from the blueprint and why
   - Open issues for v1.5 (Telegram integration, backtest harness, optional Finnhub paid upgrade decision)
   - A "first 6 weeks of paper" checklist for Brooks to monitor

## Process discipline (since the M5–M8 deferrals are exactly what M9 is paying for)

- **Do not** create a "deferred to M10" note unless you've first paused and asked Brooks. The handoff says: *"If you hit ambiguity in the blueprint that genuinely blocks you, stop and ask Brooks."* This rule exists for a reason. M5–M8 each had reasonable individual deferrals that compounded into a system that couldn't run.
- One git commit per sub-task. Conventional-commits style (`fix:`, `feat:`, `test:`, `refactor:`).
- Run `ruff check` and `mypy --strict` before each commit.
- After each sub-task, append to `logs/build_journal.md` with what was built, what surprised you, what's pending. Be honest if anything you initially planned didn't pan out.
- If `pytest` fails on any pre-existing test as a side effect of your changes, fix the test or fix your change — never disable it.

## Where to put things you can't or shouldn't do in M9

`logs/m10_backlog.md` exists as the sanctioned dumping ground. Use it for:
- Anything you'd otherwise mark "deferred to next milestone."
- Bugs you spot in M1–M8 code that aren't in scope for M9 sub-tasks 1–2.
- Improvements you genuinely think matter but would be scope creep here.
- Questions for Brooks that don't block your current sub-task.

Append to it freely with timestamped entries. Each entry: title, why it matters, rough effort estimate, suggested priority (P0 = before paper-trading, P1 = first 2 weeks of paper, P2 = whenever). Brooks reads this when planning M10.

**Do not** silently defer items inside `build_journal.md` the way M5–M8 did. The journal is for "what I built today." The backlog is for "what I noticed but did not build."

## Things you should NOT do

- Do not add features beyond M9's five sub-tasks. Telegram = M10. Backtest harness = M10. Per-agent leverage gauges + friction ledger panel = M10. Config YAML extraction = M10. Calibration deep-pass = M10. (All already pre-populated in `logs/m10_backlog.md`.)
- Do not silently change library choices.
- Do not enable real-money trading anywhere. Paper only.
- Do not add public-facing endpoints.
- Do not add new dependencies without flagging.
- Do not change the leverage system math from blueprint §16 (it's research-derived; "improvements" by intuition will desync from the journal-cited reasoning).

## When you're done with sub-task 5

Write `logs/v1_complete.md` and `logs/m9_complete.md`. The latter is your milestone-end note: what got done, what surprised you, what's open for M10. Then stop. Do not begin paper-trading on Brooks's behalf — that's his decision and his Alpaca account.

## Now

Read `logs/m1_m8_audit.md` and `logs/build_journal.md` first, then summarize the M9 plan back to Brooks in ≤200 words. Then start sub-task 1.
