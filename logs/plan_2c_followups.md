# Plan 2c — Tier 3 follow-ups

> Seeded during Plan 2c implementation. These are the next planning round's
> candidates after Plan 2c stabilizes (~30 days of paper-trading data).

## 1. News score vs. actual price move feedback loop

After ~30 days of Plan 2c data, mine the `news_items` table joined with
subsequent price-bar moves (1h, 4h, 24h) to find systematic over- or
under-reaction patterns by symbol tier and impact level. T2.2's
`scored_at` column is the join key. The output trains a calibration
adjustment for `NewsScorer.score()` — for example, "tech sector impact-4
news systematically overshoots first-hour move by ~30%; recalibrate
impact for tech sector by -1." Expressed as a per-symbol-tier scalar in
the scorer's prompt or a deterministic post-score adjustment table.

## 2. Dynamic capital reallocation acceleration

`Manager.capital_reallocation` runs every 4 weeks. With T1.5's per-sleeve
attribution providing daily evidence, consider tightening the cadence to
2 weeks once the first 8 weeks of paper-trading shows a non-degenerate
P&L spread between sleeves (Sortino dispersion across sleeves > 0.3).
The 4-week cadence is a conservative default chosen for noise tolerance;
tighter cadence is justifiable once we have evidence the noise floor is
lower than assumed.

## 2b. Per-intent realized P&L attribution for the Sunday critique (T2.4 deviation)

T2.4 (Sunday adversarial critique) was specified as "3 worst-realized-
P&L intents per sleeve" but the bot's data model doesn't support that
ordering today:

- `agent_pnl_daily` (T1.5) aggregates per (date, agent), not per intent.
- `LotLedger` discards the per-sale price on partial trims — only fully
  closed lots record `exit_price`.

To get true per-intent realized P&L would require:
- Walk every FILL_RECEIVED event in the OMS log
- For each SELL fill, look up its `exit_fill_id` in the lot ledger,
  find the matching BUY's `entry_fill_id`, then the BUY's order, then
  the originating intent_id from the order
- FIFO-match SELL qty against the queue of BUY-side lots and tally
  realized P&L per intent

That linkage is doable but sizeable (~150 LOC, plus tests). T2.4 ships
with a `conviction × target_weight` heuristic — picks the high-stakes
intents the critique prompt is calibrated for. Once the per-intent
P&L pipeline lands (Tier 3), swap `AgentMemory.top_intents_since` for
a P&L-ordered variant.

## 3. Risk_check fallback monitoring

Track the rate of `risk_check_lite` (Sonnet downgrade) fires per week.
If consistently > 0, that's evidence Plan 2c's Opus risk-check budget is
undersized for the system's actual high-conviction intent rate.
Consider promoting to a higher daily Opus risk_check ceiling (Plan 2b's
$0.25/day cap retains more headroom). Implementation: a weekly
journal section that surfaces the lite-vs-full split for the prior 7
days, plus an alert at >2 lites in a 7-day rolling window.

## 4. Anthropic cache-threshold calibration note (verified empirically 2026-05-11)

**The Plan 2c handoff cited cache-prefix minimums of Haiku=2,048,
Sonnet=1,024, Opus=2,048. The actual current minimums per Anthropic's
docs (verified by direct API call against the rendered prompts):**

| Model               | Minimum cacheable prefix |
|---------------------|--------------------------|
| `claude-haiku-4-5`  | **4,096 tokens**         |
| `claude-sonnet-4-6` | **2,048 tokens**         |
| `claude-opus-4-7`   | **4,096 tokens**         |

All four system prompts in this project were below their model's
threshold prior to T1.1 — meaning every Anthropic call in the bot's
history paid the full uncached input price. The plan's $0.10/day target
was set assuming cache hits we weren't actually getting; once T1.1
landed (all four prompts padded to ~10–46% above their thresholds),
realized cost-per-call dropped substantially. Verify against
`data/daily_spend.json` after the first full trading day.

**Operational rules going forward:**

- Every new system prompt added (e.g., the T2.2 NewsScorer and T2.3
  HaikuSynthesizer prompts) must be empirically verified to clear its
  target model's threshold via direct API call inspecting
  `usage.cache_creation_input_tokens > 0` on the first call and
  `usage.cache_read_input_tokens > 0` on the second. Do not rely on
  `messages.count_tokens` alone — token estimates and call-time input
  counts can differ by 10-20% (likely due to tool/format overhead).
- The regression test `tests/test_llm_cache.py` covers all four
  current prompts. Extend it (add the new prompt to the `_PROMPTS`
  list) before considering any new-prompt commit done.
- If Anthropic publishes a new model version mid-implementation, the
  thresholds may shift. The empirical check above is the source of
  truth — update the `_PROMPTS` table and the threshold reference
  here, do not assume continuity from the prior model.
- The `cost-shape implications` of larger cached prefixes: cache-write
  cost on the first call of each window scales with prefix size
  (~$0.0056 for a 4,500-token Haiku write); cache-hit reads cost ~10%
  of the equivalent uncached read. Net is favorable as long as call
  bursts cluster within an hour or two. If a prompt is written
  dramatically more often than read (e.g., one-shot tool invocations),
  the math inverts and caching becomes a small loss. Audit `cached=N`
  ratios in `logs/app.log` periodically to confirm the call-burst
  pattern still amortizes.
