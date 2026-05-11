# M1–M8 Audit — Multi-Agent Asset Competitive Bot

**Auditor:** Claude (Opus 4.7, 1M ctx) · subagent inside the same Claude Code session
**Date:** 2026-04-26
**Scope:** Milestones 1 through 8, as built per `CLAUDE_CODE_HANDOFF.md`

---

## Executive summary

> **TWO REAL ISSUES TO FIX BEFORE PAPER-TRADING.**
> 1. **`agents/llm.py` does not set `cache_control: {"ttl": "1h"}`.** It only sets `{"type": "ephemeral"}`, which means it gets the silently-regressed 5-minute default. The handoff called this out explicitly. Every Anthropic call is currently 12× more expensive than budgeted on cache hits across hour boundaries.
> 2. **`dashboard/app.py` binds to `0.0.0.0`**, violating the local-only privacy rule. Should be `127.0.0.1`.

The build is otherwise in good shape. All eight milestones produced runnable, tested code: 38 source files, ~7.3K source LOC, ~6K test LOC, 406 tests reported passing per the build journal (could not re-run — cowork sandbox is Python 3.10, project requires 3.12 with no network for upgrade). The OMS, FSM, kill switch, sizing, lots, wash-sale checker, budget ledger, AlpacaBroker adapter, reconciler, and four-agent system are all implemented faithfully to spec, with thoughtful test coverage on the load-bearing modules. Prompts are byte-for-byte identical to `blueprint/prompts/`. Manager has all 7 call types including `mc_proposal`. Leverage math (per-agent base caps, EWMA λ=0.94, 8% floor, 1.75× cap, ±10% day cap, VIX ladder, drawdown ladder) is implemented exactly as written.

The biggest *honest* gap is that **M8 was declared "Build complete" but the system cannot actually be started end-to-end.** There is no `app.py` (the entrypoint the blueprint and README both demand). There is no scheduler wiring, no heartbeat writer, no ops/journal writer, no `logs/v1_complete.md`. The dashboard runs standalone, the agents run standalone, the OMS runs standalone — but nothing runs them together. M9 must close that gap before paper-trading begins.

Bottom line: **needs fixes first** (small, ~half-day of work).

---

## What got built — file tree comparison vs. blueprint §10

### Present and matching spec
- `pyproject.toml` ✓ (Python 3.12, ruff + mypy strict on core/agents/execution, full dep list)
- `.env.example` ✓
- `README.md` ✓ (still describes pre-build state — slightly stale)
- `core/{events,state_machine,clock,types}.py` ✓
- `data/{market,news,store,cache,summarize}.py` ✓
- `agents/{base,haiku_agent,sonnet_agent,opus_agent,manager_agent,memory,llm,calibration}.py` ✓
- `agents/prompts/{haiku,sonnet,opus,manager}_agent.md` ✓ (identical to blueprint/prompts)
- `execution/{risk,sizing,oms,broker,reconciler,kill_switch,approval_queue,lots,tax}.py` ✓
- `execution/{alpaca_broker,fake_broker,oms_store,budget}.py` ✓ (extras, all sensible)
- `dashboard/{app,layout,data}.py` ✓ (note: `app.py` not `server.py` as in §10 — minor)
- `tests/test_*.py` ✓ (22 test files, well-organized)
- `config/{__init__,settings}.py` ✓
- `ops/telegram.py` ✓ (stub)
- `logs/build_journal.md` ✓

### Missing vs. blueprint §10
- **`app.py` (project root).** The entrypoint. Without it the system is a kit, not a bot. Build journal (M6 pending, M7 pending, M8 silent) flags this repeatedly.
- **`config/agents.yaml`** — per-agent model, sleeve %, prompt path. Hardcoded inside agent classes instead.
- **`config/universe.yaml`** — tradable symbols, blocklist, sector tags. Hardcoded inside Haiku agent (`_EQUITY_UNIVERSE`).
- **`config/schedules.yaml`** — cron-like job spec.
- **`config/tax.yaml`** — bracket assumptions, harvesting rules.
- **`backtest/engine.py`** + **`backtest/metrics.py`** — entire `backtest/` directory exists but is empty. Vectorbt is in deps, never used.
- **`dashboard/components/`** — empty directory. Components are inlined in `dashboard/layout.py` (acceptable for current size, but no `equity_chart`, `positions_table`, `memos_feed`, `approval_inbox` modules).
- **`dashboard/theme.css`** — styles inlined in component dicts instead.
- **`ops/alerts.py`** — ntfy adapter is missing despite being in the spec and `NTFY_TOPIC` being in `.env.example`.
- **`ops/heartbeat.py`** — KillSwitchEngine has `record_heartbeat`/`check_heartbeat` but no module writes ticks.
- **`ops/journal.py`** — daily/weekly Markdown report generator. Manager produces journal text in-process but nothing writes it to `logs/`.
- **`ops/schedules.py`** — no APScheduler wiring (despite `apscheduler` being in deps).
- **`tests/fixtures/`** — empty directory.
- **`src/`** — empty directory left over from the pre-build skeleton; harmless but should be removed.

### Unexpected additions (all sensible)
- `agents/json_utils.py` — shared JSON-with-fallback parser used by all 4 agents. Good factoring.
- `execution/fake_broker.py` — required for testing; called out in handoff.
- `execution/oms_store.py` — append-only event log, blueprint Principle 5.
- `execution/budget.py` — `daily_spend.json` enforcer, blueprint Principle non-negotiable #4.
- `dashboard/data.py` — testable read-only adapter; clean separation.

---

## Test + lint status

**Could not run live:** cowork sandbox is Linux with only Python 3.10; project requires 3.12. Network calls blocked, so `uv python install 3.12` failed. The project's `.venv/` was built on macOS and has hard-coded interpreter paths that don't resolve in Linux.

**Per the build journal (M8 closing entry):**
> 406 tests pass. ruff clean. mypy --strict clean across 38 source files.

Spot-checked test counts manually (`grep -c "^\s*def test_"`):

| File | def test_ count | Journal claim |
|---|---|---|
| test_state_machine.py | 48 | 48 (M1) |
| test_oms.py | 19 | 19 (M2) |
| test_oms_recovery.py | 15 | 15 (M2) |
| test_oms_store.py | 13 | 13 (M2) |
| test_fake_broker.py | 20 | 20 (M2) |
| test_lots_fifo.py | 17 (counted: 12 `def`, 5 paramed) | 17 (M3) |
| test_wash_sale.py | 14 | 14 (M3) |
| test_kill_switch.py | 35 (counted: 26 `def`, ~9 paramed) | 35 (M3) |
| test_sizing.py | 30 (19 `def` + parametrize) | 30 (M3) |
| test_risk_gate.py | 24 | 24 (M3) |
| test_budget_enforcer.py | 12 | 12 (M3) |
| test_alpaca_broker.py | 19 | 19 (M4) |
| test_reconciler.py | 9 | 9 (M4) |
| test_market_data.py | 15 | 15 (M5) |
| test_data_store.py | 16 | 16 (M5) |
| test_news_adapters.py | 15 | 15 (M5) |
| test_data_cache.py | 8 | 8 (M5) |
| test_summarize.py | 6 | 6 (M5) |
| test_agents_offline.py | 28 | 28 (M6) |
| test_agents_m7.py | 27 | 27 (M7) |
| test_dashboard_data.py | 21 | 21 (M8) |

Counts match the journal exactly. Coverage was 90% on `execution/` per M2; no later milestone re-reported coverage.

**LOC:** 13,358 total; ~7,274 source / ~6,084 test. Source overshoots the §10 5K target — most of the bloat is in `execution/oms.py` (~470 LOC) and the four agent files. Reasonable for what got built; not concerning.

**Lint/type:** I cannot re-verify without 3.12. The build journal claims clean; the code I read uses modern type syntax consistently and has `# type: ignore[union-attr]` only at obvious union-narrowing sites.

---

## Per-module fidelity audit

| Module | Blueprint requirement | Actual status | Notes |
|---|---|---|---|
| `core/state_machine.py` | Generic FSM, guards/actions, crash-safe replay | ✓ FULL | Order FSM with 8 states, 14 arcs |
| `core/events.py` | EventBus pub/sub, 13 event types | ✓ FULL | Wildcard `*` channel for dashboard |
| `core/clock.py` | Clock protocol, WallClock, BacktestClock | ✓ FULL | 2026 NYSE holidays hardcoded |
| `core/types.py` | Intent, Order, Fill, Lot, Position, NewsItem, AgentMemo + enums | ✓ FULL | Frozen dataclasses; mutations via `replace()` |
| `execution/oms.py` | FSM + append-only event log + reconciliation | ✓ FULL | RLock for re-entrant callbacks; persist→update→publish ordering correct |
| `execution/oms_store.py` | SQLite WAL, durable on append | ✓ FULL | Append-only EventKind log, JSON serialization |
| `execution/risk.py` | All RiskGate Layer 1 checks | ⚠ PARTIAL | See deviations table below |
| `execution/sizing.py` | Vol-target EWMA λ=0.94, 8% floor, 1.75× cap, ±10% day cap | ✓ FULL | Per-agent base caps, VIX ladder, DD ladder all match §16 |
| `execution/kill_switch.py` | -15/-25/-33 ladder, -2% intraday, per-agent bench, heartbeat | ✓ FULL | One-directional escalation correct |
| `execution/lots.py` | FIFO/LIFO ledger | ✓ FULL | Threadsafe; cross-agent isolation |
| `execution/tax.py` | Wash-sale checker, harvesting candidates | ✓ FULL | But not wired into `risk.check_intent()` — see deviations |
| `execution/budget.py` | `daily_spend.json` enforcement | ✓ FULL | UTC date-keyed; auto-reset on rollover |
| `execution/broker.py` + `alpaca_broker.py` + `fake_broker.py` | Broker Protocol, Alpaca + Fake adapters, idempotent on `client_order_id` | ✓ FULL | 422-idempotency fallback; multi-leg unwind not yet exercised |
| `execution/reconciler.py` | 60s loop, 1-share or $1 mismatch → halt | ⚠ PARTIAL | Qty drift implemented; **dollar-mismatch missing** (deferred per build journal) |
| `agents/llm.py` | Budget gate, retry on 529, **explicit 1h cache TTL**, max_tokens caps | ⚠ PARTIAL | **Cache TTL is implicit 5m, not explicit 1h.** Retry catches `RateLimitError` (429) and `APIStatusError` (covers 529) but only one retry with 1s sleep on 5xx — should match 429's exponential+jitter |
| `agents/memory.py` | SQLite memories, journals, intent_log | ✓ FULL | `recent_intents_rows` added in M8 for dashboard |
| `agents/calibration.py` | Brier score, conviction buckets | ✓ FULL | |
| `agents/haiku_agent.py` | GTAA + crypto trend; SMA/momentum signals | ✓ FULL | 4 intents max; LIQUIDATE guard; equity SMA 210d, crypto SMA 50d / 14d momentum |
| `agents/sonnet_agent.py` | Multi-factor; 12-1 momentum proxy | ✓ FULL | 5 intents max; trim/add/exit action mapping |
| `agents/opus_agent.py` | Concentrated GARP + Thu/Fri deep-dive method | ✓ FULL | `deep_dive()` method exists, but **no scheduler invokes it** |
| `agents/manager_agent.py` | All 7 call types (incl `master_capability_proposal`) | ✓ FULL | Class docstring still says "six" — minor staleness |
| `agents/prompts/*.md` | Match blueprint/prompts | ✓ FULL | Byte-for-byte identical |
| Per-agent leverage caps (Haiku 1.5×, Sonnet 1.25×, Opus 1.0×) | §16 | ✓ FULL | `AGENT_BASE_MAX_GROSS` exact |
| VIX ladder | §16 (<12 0.6 / 12–18 1.0 / 18–25 0.8 / 25–35 0.5 / >35 0.25) | ✓ FULL | `classify_vix` + `VIX_SCALARS` exact |
| Drawdown ladder | §16 (NORMAL/YELLOW/ORANGE/RED/FORCED_CASH 1.0/0.75/0.5/0.25/0.0) | ✓ FULL | `classify_drawdown` + `DRAWDOWN_SCALARS` exact |
| LETF whitelist + 5d max-hold | §16 | ✓ FULL | TQQQ/SQQQ/UPRO/SPXU/SOXL/SOXS/TMF/TMV; `check_letf_auto_liquidations()` separate method |
| Options whitelist (defined-risk only) | §16 | ⚠ PARTIAL | Only the 20%-of-sleeve cap is enforced. **No structural check that an option order is a defined-risk multi-leg** (verticals/condors/CC/CSP) vs. naked. Naked-call detection deferred. |
| 5-day LETF reopen anti-rotation rule | §16 (>2 reopens / 15 days flagged) | ✗ MISSING | Not implemented |
| Master capability slider | Dashboard top strip | ⚠ PARTIAL | Displayed as static `_strip_cell` value, **not a `dcc.Slider`**. Cannot be moved at runtime from the dashboard. |
| `effective_max_gross` per-agent display | §11 | ⚠ PARTIAL | Top strip shows one global "MAX GROSS" value; per-agent gauges not built |
| "Leverage Budget Used" gauges per agent | §11 | ✗ MISSING | |
| Friction ledger panel | §11 (added in v0.4) | ✗ MISSING | |
| Dashboard polling at 3s | §11 / Principle 9 | ⚠ DEVIATION | `POLL_INTERVAL_MS = 5000`, not 3000. Documented inconsistency with the docstring above the constant |
| Dashboard binds local-only | "no public-facing endpoints" | ✗ VIOLATION | `host="0.0.0.0"`. Must be `127.0.0.1` |
| `app.py` entrypoint | §10 + README | ✗ MISSING | No way to start bot loop + dashboard cleanly |
| ntfy.sh alerts | Stack section in handoff | ✗ MISSING | `ops/alerts.py` not built; only Telegram stub exists |
| Heartbeat writer | §10, kill-switch precondition | ✗ MISSING | `ops/heartbeat.py` not built. KillSwitchEngine accepts heartbeats; nothing emits them |
| Daily/weekly journal writer | §10 | ✗ MISSING | `ops/journal.py` not built. Manager produces journal text in memory; never persisted to `logs/WEEK_NN.md` |
| APScheduler wiring | §10 + handoff | ✗ MISSING | Dep in `pyproject.toml`; never imported |
| Backtest harness (vectorbt) | §10, M5 in handoff (paired with rules-only baseline) | ✗ MISSING | Whole `backtest/` dir empty. Real-money graduation gate (§13) requires LLM-vs-rules comparison; without this, graduation is impossible |
| `config/{agents,universe,schedules,tax}.yaml` | §10 | ✗ MISSING | Hardcoded constants instead. `tax.yaml` matters because tax bracket assumptions are not configurable |

---

## Dangerous deviations found

These need attention before paper-trading begins:

1. **Cache TTL not explicit 1h** *(`agents/llm.py:111-117`)*
   The `cache_control` block sets `{"type": "ephemeral"}` only. The handoff's non-negotiable rule #2 says: *"Every Anthropic call must be cached (`ttl: "1h"` explicitly — Anthropic silently regressed the default to 5m)."* This is a budget-busting bug. Each agent runs more often than every 5 minutes only on news triggers, but the system prompt + tools cache prefix is paid for fresh on every call past the 5-minute mark. Fix is one line:
   ```python
   "cache_control": {"type": "ephemeral", "ttl": "1h"},
   ```
   No tests catch this — would have to add one that inspects the SDK call args.

2. **Dashboard binds to all interfaces** *(`dashboard/app.py:85`)*
   `app.run(host="0.0.0.0", ...)` with a `# noqa: S104` suppressing the bandit warning. Brooks's privacy rule is "everything is local-only." Change to `127.0.0.1` and remove the noqa.

3. **Wash-sale check declared as RiskGate dependency but never invoked** *(`execution/risk.py:96-165`)*
   `WashSaleChecker` is a `__init__` parameter (`self._wash`) but `check_intent` never calls `self._wash.is_blocked(...)`. M3 build journal flags this: *"wash-sale check is wired but not yet called in `check_intent()`. Will activate in M4..."* M4 came and went. Tax-aware mandate is currently advisory only.

4. **No `app.py` — the system cannot be started**
   Every milestone-end note from M5 onward defers integration. M8's "Build complete" headline is misleading. The dashboard, OMS, agents, broker, and reconciler are all importable but no module owns the lifecycle.

5. **No process-level enforcement that a budget-exhausted day trips the kill switch.**
   `BudgetLedger.is_exhausted()` exists. `KillSwitchEngine.trip_budget_exhausted()` exists. Nothing wires them. M3 journal: *"Budget ledger is standalone; wired to KillSwitchEngine.trip_budget_exhausted() will happen in M5 orchestration layer."* M5 came and went; no orchestration layer was built.

6. **Reconciler dollar-mismatch missing** *(`execution/reconciler.py`)*
   Principle 4 requires "1-share OR $1 mismatch → halt." Only share drift is checked. M4 journal acknowledges and defers to M6, which didn't pick it up.

7. **MASTER_CAPABILITY slider is read-only on the dashboard.**
   It's a `_strip_cell` displaying the env-var value, not a `dcc.Slider` callback that writes back to settings. Brooks asked for a slider; got a label.

8. **Options policy does not check structure.**
   The 20%-of-sleeve cap is enforced. The blueprint §16 ban on "naked anything (including naked long calls)" is not enforced — `risk.py` doesn't introspect option leg structure. Today this matters less because no agent currently emits MLEG orders, but it's a footgun if/when they do.

9. **No retry of LLM calls on 529 with backoff** — `APIStatusError` catches it but does only one 1s-sleep retry, not the exponential+jitter that `RateLimitError` gets. The handoff lists 529 retry as a M2 (v0.2) addition.

### Things that are **NOT** dangerous (sniffed for, didn't find)

- ✓ No real-money paths anywhere. `alpaca_paper: bool = True` default; no `paper=False` literal in source.
- ✓ No LLM-side position sizing or cost calculation. Agents only set `target_weight`; everything else is deterministic Python.
- ✓ No public credentials in source. `.env.example` has only placeholders.
- ✓ No tests asserting buggy behavior — spot-checked test_risk_gate, test_sizing, test_oms_recovery; all assertions are correct.
- ✓ Prompts are byte-identical to `blueprint/prompts/`. No drift.

---

## Build journal highlights

The journal is unusually detailed and honest. Worth reading in full. Notable quotes:

> *(M2)* "The crash-recovery test 'crash after broker call, before ACCEPTED logged' required hand-crafting the OMS state... since the OMS is too well-designed to crash there in normal operation. That's a feature, not a bug."

> *(M2)* "Coverage on `execution/`: 90%."

> *(M3)* "`sum()` on an empty generator returns `Literal[0]` (int) not `Decimal` — mypy --strict catches this." — exactly the kind of detail that says someone actually ran the type-checker.

> *(M3, flagged but never escalated)* "wash-sale check is wired (WashSaleChecker is a dependency) but not yet called in `check_intent()`. Will activate in M4 once OMS feedback loop provides loss-sale signals." → M4 didn't.

> *(M3, flagged but never escalated)* "Budget ledger is standalone; wired to KillSwitchEngine.trip_budget_exhausted() will happen in M5 orchestration layer." → M5 didn't.

> *(M4)* "Position reconciliation tolerance is 1 share (configurable). Blueprint Principle 4 says '1-share or $1 mismatch flips to RECONCILIATION_BREAK'. Dollar mismatch is not yet implemented — deferred to M6 when we have real-time pricing." → M6 didn't.

> *(M6)* "M6 is 'Haiku end-to-end' — the full loop (market data → Haiku → OMS → fill → reconcile) is not yet wired into a single `app.py` entrypoint. That wiring happens in M7/M8." → M7 deferred to M8; M8 deferred to M9.

> *(M7)* "App entrypoint (`app.py` or `main.py`) wiring all four agents into a scheduled loop (APScheduler, Alpaca streaming, market-hours awareness)" — listed as pending. Never built.

> *(M8 closing)* "All eight milestones land. **406 tests pass. ruff clean. mypy --strict clean across 38 source files.** ... Next: the ops layer (`app.py` scheduler, Telegram alerts, recovery cron) and graduation criteria evaluation."

The journal also includes one decision worth re-litigating: M2's flagged-but-cute line *"`BrokerRejection` exceptions intentionally do NOT have an `Error` suffix (per N818 ruff rule) — they're domain concepts in a finance system. Suppressed N818 in pyproject.toml."* Defensible but worth knowing.

### What Claude Code never escalated as "stop and ask Brooks"

The handoff said: *"If you hit ambiguity in the blueprint that genuinely blocks you, stop and ask Brooks rather than guessing."* The journal contains zero such pauses. Instead, it has roughly five "deferred to next milestone" notes that quietly accumulated into the wash-sale, dollar-mismatch, app.py, scheduler, and ops layer all being missing at the end. Each individual deferral was reasonable; the net effect is a system that can't run.

---

## Open questions Claude Code flagged but didn't escalate

1. Does the LLM cache TTL need to be explicit 1h to match handoff? *(my answer: yes, fix it)*
2. Should the dashboard MC slider actually write back to runtime, or stay env-controlled? *(blueprint says slider — implement as `dcc.Slider` with a callback to a settings store)*
3. Should `app.py` start the dashboard, or should they be two terminals? *(blueprint §9: "foreground `python app.py` in a regular Terminal window" — suggests one process, one terminal; dashboard is a thread or subprocess)*
4. How does `MASTER_CAPABILITY` propagate when changed mid-run? Currently nothing rereads `settings.master_capability` between calls.
5. The Manager's `weekly_journal()` returns markdown. Where does it get written, and who notifies (ntfy/Telegram)?
6. The OpusAgent `deep_dive()` method exists but no scheduler triggers it on Thu/Fri.
7. No A/B baseline harness (rules-only vs LLM sleeve) exists. Real-money graduation §13 explicitly requires LLM to beat rules-only on max DD and DD duration. Without this, graduation criteria can't be evaluated.

---

## Recommended scope for M9

Three options, ranked by my read of risk-vs-value.

### Option A (RECOMMENDED) — "make it actually run, then start paper-trading"

The conservative path. Estimated 1.5–2.5 days of Claude Code work.

1. **Fix the four bugs.** Cache TTL → `"ttl": "1h"`; dashboard host → `127.0.0.1`; poll interval → 3000ms; dashboard MC display → `dcc.Slider`. ~1 hour.
2. **Wire the deferred plumbing.** Activate wash-sale check in `risk.check_intent()`. Add dollar-mismatch to reconciler. Wire `BudgetLedger.is_exhausted()` to `KillSwitchEngine.trip_budget_exhausted()` in the orchestration loop. ~3 hours.
3. **Build `app.py`.** APScheduler-driven. Owns: market-hours gate; Haiku every 30m during market hours + on volatility triggers; Sonnet 2× daily; Opus daily lite + Thu/Fri deep-dive cron; Manager weekly Friday journal + 4-week reallocation; reconciler thread; heartbeat writer; dashboard subprocess; budget reset at UTC midnight. ~6–8 hours.
4. **Build `ops/alerts.py`** (ntfy) and `ops/journal.py` (writes Manager output to `logs/WEEK_NN.md`). ~2 hours.
5. **Smoke test for one trading day** (paper). Write `logs/v1_complete.md` per the handoff. ~1 trading day elapsed.

Then start the 6-week paper-trading observation period. Telegram, backtest harness, polish all defer to M10.

**Why this:** the blueprint's whole point is the 6-week observation gate before real money. Every day not paper-trading is a wasted week of evidence. The current backlog is small and well-understood; clearing it is a much higher-value use of Claude Code time than adding scope.

### Option B — "Telegram + light v1.5 polish"

For if Brooks judges the missing pieces in (A) as small enough to do himself.

1. Build out `ops/telegram.py` (replace stub with real Bot API integration).
2. Add per-agent leverage gauges + friction ledger to dashboard (§11 v0.4 spec).
3. Build the rules-only baseline backtest harness (`backtest/engine.py` + `metrics.py`) since it's needed for §13 graduation.
4. Add `config/*.yaml` files for the hardcoded constants.

This is more "feature work" and less "make it run." Risk: paper-trading start date slips.

### Option C — "build the A/B baseline first" *(non-obvious)*

The deepest thinking option. Before a single paper trade, build the vectorbt rules-only baseline and freeze it. Reasoning: §13 graduation requires LLM to beat rules-only baseline on max-DD and DD-duration. Without a frozen baseline running in parallel from day 1 of paper, the comparison is post-hoc and the graduation decision becomes a vibe call, not an evidence call. The mathematician lens (CHANGELOG v0.2) flagged this. Estimated 2–3 days.

The cost: paper-trading delayed by another sprint. The benefit: 6 weeks from now, the graduation question has a defensible answer.

If Brooks's tolerance is "let's just run it and see," do A. If Brooks wants to be able to honestly answer "did the LLMs beat rules-only?" in 6 weeks, do C.

---

## Bottom line

**Needs fixes first** — half a day of work to be ready to paper-trade. M1–M8 produced careful, well-tested deterministic infrastructure. The gap is integration: `app.py` was never built, four small bugs accumulated through the milestone deferrals, and the ops layer (alerts, heartbeat, journal writer, scheduler) is missing. None of it is hard. M9 = "Option A" above.
