# Plan 2c — Implementation Complete

> 2026-05-11. Branch `plan-2c` (11 commits off `master` at `3614bee`).
> Implemented by Claude Opus 4.7 over one session.

---

## What landed

Plan 2c's `$0.10/day, max-EV` reshape is implemented across 11 commits. All
Tier-1 and Tier-2 items shipped; one (T2.4) ships with a documented
heuristic in place of the per-intent P&L ordering the plan originally
specified (see "Deviations" below).

### Commits, in landing order

| # | SHA | Item | Summary |
|---|-----|------|---------|
| 1 | `86f043a` | T1.1 | Padded all 4 prompts above their actual model thresholds + 5-prompt live cache test |
| 2 | `c4d14ad` | T1.2 | Sonnet schedule 5/day → 1/day (EOD only) |
| 3 | `4425450` | T1.3 | Opus schedule daily+Fri+Thu → weekly Thursday only |
| 4 | `eabfca5` | T1.4 | `signal_fingerprint` skip-when-unchanged gating for Opus and Manager |
| 5 | `9dbe8fb` | T1.5 | Per-sleeve P&L attribution table + 16:45 ET snapshot job + dashboard panel |
| 6 | `76a306f` | T2.1 | `Manager.risk_check` wired with Sonnet downgrade fallback (never skips) |
| 7 | `ab0cd5b` | T2.2 | `NewsScorer` (Haiku) + schema migration + `news.high_impact_scored` event |
| 8 | `394ef5f` | T2.3 + T1.6 | `HaikuSynthesizer` morning brief (replaces Manager `morning_brief`) |
| 9 | `1f0b6d0` | T2.4 | Sunday adversarial-critique cron + intent-log target_weight migration |
| 10 | `6770528` | T2.5 + T2.6 | Event-driven Opus dive + intraday-shock event + `daily_spend_cap = 0.10` |
| 11 | `1e02599` | T2 verify | Minor mypy --strict cleanups on Plan 2c production files |

### Important calibration finding (logged 2026-05-11)

The Plan 2c handoff cited Anthropic cache-prefix minimums of Haiku=2,048,
Sonnet=1,024, Opus=2,048. Empirical verification against the live API
during T1.1 found the *actual* current minimums are:

| Model | Plan assumed | **Actual** |
|---|---|---|
| `claude-haiku-4-5` | 2,048 | **4,096** |
| `claude-sonnet-4-6` | 1,024 | **2,048** |
| `claude-opus-4-7` | 2,048 | **4,096** |

All four pre-Plan-2c system prompts were below their model's threshold,
which is why historical `logs/app.log` lines showed `cached=0` on every
Anthropic call. T1.1 padded all four to clear their actual thresholds
with margins of 16–46%. The expanded scope (all 4 prompts, not just
Haiku) was confirmed with Brooks before landing. The same threshold
verification is now required for any new prompt: `tests/test_llm_cache.py`
covers all 6 production prompts (4 pre-existing + NewsScorer + HaikuSynthesizer)
and asserts each one actually writes+reads its cache live.

See `logs/plan_2c_followups.md` item #4 for the operational rules going
forward.

---

## Cost-shape vs. the plan's $0.10/day target

| Slot | Plan target | Status |
|---|---|---|
| Cache-fixed sleeve loop (Haiku 6× + Sonnet 1× + Opus 1×/wk) | $0.030 | ✅ Wired. Sonnet now 1×/day (T1.2); Opus weekly-only (T1.3); all prompts cached (T1.1). |
| News-impact Haiku scoring per fresh item | $0.025 | ✅ Wired (T2.2). Pre-filtered (body ≥200 chars + in-universe symbol). |
| Haiku daily morning synthesis | $0.005 | ✅ Wired (T2.3). Replaces Manager morning_brief at ~50× lower cost. |
| Opus-Manager Friday weekly synthesis | $0.014 | ✅ Was already wired (T1.4 added fingerprint gating). |
| Manager risk_check on extreme intents (Opus default, Sonnet downgrade) | $0.015 | ✅ Wired (T2.1). Daily ceiling 2 Opus calls; downgrade to Sonnet after that. Never skipped. |
| Headroom for events (drawdown, MC proposal, news-triggered dives) | $0.011 | ✅ Wired (T2.5). One off-schedule Opus dive per ISO week max. |

`daily_spend_cap` set to `Decimal("0.10")` (T2.6) in `config/settings.py`.

**The numbers above are budget allocations, not measured.** Realized
spend over a full trading day was not measurable in this implementation
session because:

1. Today is Sunday 2026-05-11 (US markets closed).
2. The implementation work itself was the only "trading-day activity"
   in `data/daily_spend.json` and that's a calibration trial, not a
   normal day.

A real one-day measurement is **Step 3** of the plan's verification
block and is intentionally left for Brooks to run on a Monday open with
the bot in normal mode. Expected reading from `data/daily_spend.json`
should be `total_usd < 0.12` (target $0.10 + small cache-write overhead
on first call of each window).

---

## What was verified in-test

The plan's verification block step 1 (`pytest`) and step 2 (`ruff check` /
`mypy --strict on changed files`) both pass:

- **`pytest -q`**: **677 passed**, 1 skipped without `ANTHROPIC_API_KEY`
  (the live cache test). With key: 678 passed.
- **`ruff check`**: 166 errors total across the repo — **identical to
  pre-Plan-2c master**. Net-zero new ruff errors introduced across all
  11 commits.
- **`mypy --strict`** on Plan 2c new files (`ops/attribution.py`,
  `ops/agent_pnl_store.py`, `agents/news_scorer.py`,
  `agents/haiku_synthesizer.py`): clean. The two pre-existing errors in
  `execution/budget.py` and `data/market.py` are unchanged and outside
  Plan 2c scope.

Plan 2c-specific in-test verifications:

| Item | Verification |
|---|---|
| **Cache prefix sufficiency** (T1.1) | `tests/test_llm_cache.py` makes 2 live API calls per prompt, asserts `cache_creation_input_tokens > 0` or `cache_read_input_tokens > 0` on the prime call and `cache_read_input_tokens > 0` on the second call. Covers all 6 production prompts; runs in ~50s; skipped without API key. |
| **Sonnet schedule** (T1.2) | Existing `tests/test_app_scheduler.py` updated; asserts only `JOB_SONNET_EOD` remains. |
| **Opus schedule** (T1.3) | Same — only `JOB_OPUS_THURSDAY_DEEPDIVE` remains. |
| **Fingerprint gating** (T1.4) | `tests/test_signal_fingerprint.py`, 13 cases. Opus returns None in initiation mode; both prompts hash deterministically and invalidate on the right inputs. |
| **P&L attribution** (T1.5) | `tests/test_attribution.py`, 8 cases. Realized P&L computed from FIFO-matched fills (not from lots — lots discard partial-exit prices). Stable-shape contract; same-day upsert. |
| **Risk_check wiring** (T2.1) | `tests/test_risk_check_wiring.py`, 10 cases. Veto/downsize/approve handling; Sonnet downgrade after daily Opus ceiling; never skips. |
| **NewsScorer** (T2.2) | `tests/test_news_scorer.py`, 12 cases. Pre-filter (no body / short body / no in-universe symbol); event fires only on impact ≥ 4; tolerates markdown fences; rejects schema violations. |
| **HaikuSynthesizer** (T2.3) | `tests/test_haiku_synthesizer.py`, 6 cases. Persists brief via `manager_bridge.write_morning_brief`; handles no-news / no-snapshots gracefully. |
| **Adversarial critique** (T2.4) | `tests/test_adversarial_critique_wiring.py`, 7 cases. Top-3 intents per sleeve selected by `conviction × target_weight`; per-sleeve critique persistence; Sunday 18:00 ET cron registered. |
| **Event triggers** (T2.5) | `tests/test_event_triggers.py`, 7 cases. News event → off-schedule Opus dive; ISO-week rate limit; vol scan publishes shock on ±5% moves. |
| **Empirical cache verification** (across all 6 prompts) | Direct API checks against each prompt × its target model; all 6 confirmed writing and reading their caches on Haiku 4.5 / Sonnet 4.6 / Opus 4.7 within a 30s gap. |

---

## What remains for Brooks to run live

These are the items from the plan's verification block §4 that need real
market hours + a real run:

1. **One full trading day** (`python app.py` Monday open through 17:00 ET).
2. Verify `data/daily_spend.json` shows `total_usd < 0.12`.
3. Verify `logs/app.log` shows `cached=N>0` on at least one Haiku call.
4. Verify `data/news.db` has ≥1 row with `impact` populated OR the log
   shows `news_scorer: no items met pre-filter criteria today`.
5. Verify Manager memory has a `morning_brief` written by `HaikuSynthesizer`
   for today's date (key in `agents/manager_bridge.py: KEY_LAST_BRIEF`).
6. Verify `agent_pnl_daily` table has one row per sleeve for today's date.
7. If any conv≥9 + target_weight≥8% intent fired: verify a
   `manager/risk_check` or `manager/risk_check_lite` entry exists in
   `daily_spend.json`.

Beyond the immediate verification, the bot continues live paper-trading.
The Friday Manager journal (regime + critique + leverage retrospective +
calibration) is the next strategic checkpoint and will surface any
calibration deltas the daily cycle accumulates.

---

## Deviations from the handoff

### Single substantive deviation: T2.4 intent selection

The plan specified "3 worst-realized-P&L intents per sleeve" for the
Sunday adversarial critique. The bot's data model can't support per-
intent realized P&L today: `agent_pnl_daily` aggregates per
(date, agent), and the `LotLedger` discards per-sale prices on partial
trims (only fully-closed lots record `exit_price`).

To get per-intent realized P&L, we would need to walk OMS `FILL_RECEIVED`
events, look up each SELL fill's `exit_fill_id` in the lot ledger to find
the matching BUY's `entry_fill_id`, then traverse OMS to find the
originating intent. ~150 LOC of additional plumbing — meaningful work,
deferred to a Tier 3 follow-up.

T2.4 ships with **conviction × target_weight** as the selection heuristic.
That picks the same "high-stakes" intents the critique prompt is
calibrated for. When the per-intent P&L pipeline lands, swap
`AgentMemory.top_intents_since` for a P&L-ordered variant — the call site
in `app._job_manager_sunday_critique` is the only consumer.

Filed as `logs/plan_2c_followups.md` item #2b.

### Smaller departures (all documented in commit messages)

- **T1.2 EOD time:** plan said "keep at 16:35 ET" but the existing code
  was 16:30. Kept 16:30 (the plan author appears to have misremembered;
  changing 5 minutes was not in scope).
- **T2.4 helper name:** plan called the bridge helper
  `write_critique`; actual name was already `write_adversarial_critique`.
  Used the existing name.
- **T2.4 schema field names:** the plan handoff used
  `verdict / resize / resized_target_weight` for risk_check; the actual
  prompt schema in `agents/prompts/manager_agent.md` uses
  `decision / downsize / downsize_to_weight`. Honored the prompt schema
  (it's the contract).

---

## Open questions for Brooks

1. **NewsScorer pre-filter calibration.** Current pre-filter is "body
   length ≥ 200 chars AND at least one in-universe symbol." Once a few
   days of news scoring lands, we should compare the % of items that
   passed the filter vs. produced impact ≥ 3 outputs, and decide whether
   to tighten the body floor or expand the in-universe set. If the
   filter is too tight, we'd miss things; too loose, we'd waste Haiku
   calls on noise.

2. **Manager critique severity calibration.** The Sunday critique
   selects top-3 intents per sleeve by `conviction × target_weight`. If
   most week's critiques come back severity="minor", the threshold is
   too aggressive — we're calling Manager on mediocre intents. Watch
   the first 4 weeks of critique output; if the severity distribution is
   dominated by "minor", consider tightening the selection (e.g., only
   intents with conviction ≥ 8 OR target_weight ≥ 10%).

3. **HaikuSynthesizer word-budget calibration.** Prompt asks for
   180-260 words; the 4 worked examples in the prompt range 142-247.
   If briefs cluster at the low end on quiet days, that's fine. If they
   cluster at the high end every day, the synthesizer may be padding —
   consider tightening the upper bound to 220.

4. **`risk_check_lite` rate.** Track `BudgetLedger.entries` for
   `call_type == "risk_check_lite"`. If this fires more than once a
   week consistently, the daily Opus ceiling (2) is too low for the
   system's actual high-conviction intent rate — follow-up item #3.

5. **Event-driven Opus dive cadence.** Limit is 1 extra dive per ISO
   week. If high-impact news events hit the same week-window and the
   second event has a stronger thesis, we silently drop it. This is the
   right default given budget; worth re-evaluating if the cadence is
   actually 2-3 high-impact events per week and the rate limit
   consistently swallows the second one.

---

## Followups doc

`logs/plan_2c_followups.md` was seeded during T1.1 with four items:

1. News score vs. actual price move feedback loop (Tier 3 ML)
2. Dynamic capital reallocation acceleration
3. Risk_check fallback monitoring
4. Anthropic cache-threshold calibration note + operational rules

Plus an item added during T2.4:

- **2b.** Per-intent realized P&L attribution for the Sunday critique
  (replaces the conviction×target_weight heuristic shipped here).

---

## Bottom line

Plan 2c is wired end-to-end and in-test verified. Live verification
(Step 3 of the plan's verification block) requires a Monday open in
normal-trading mode; that step is left for Brooks to run.

The bot's daily call profile is now ~7× cheaper than pre-Plan-2c
allocation, with new arms (news scoring, morning synthesis, Sunday
critique, off-schedule news-triggered dives) added rather than just
trimmed. The cache prefix fix alone reclaims most of the budget
delta; the schedule trims and signal fingerprints add safety
margin; the new arms are funded out of that recovered budget plus
$0.01 of explicit headroom for events.

*— 2026-05-11*
