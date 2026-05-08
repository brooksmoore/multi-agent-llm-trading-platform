# Post-paper-trade punch list

Items deliberately deferred from the 2026-05-08 audit pass until at least
one week of live paper-trade data has accumulated. None are blocking;
all should be revisited with the actual fill log + Manager friction
ledger in hand.

## 1. Calibrate slippage_bps against real fills

Status: tier classifications + per-symbol bps lookup landed in
`config/universes.py`; surfaced via `IntentSizedEvent.estimated_slippage_bps`
and planner logs. NOT yet subtracted from `position_value` in sizing math.

After ~1 week of paper trading:
1. Pull `Fill` events from the OMS event log + Alpaca's reported
   `filled_avg_price` per order.
2. Compute realized slippage = `(filled_avg_price - mark_at_submission) /
   mark_at_submission * 10000` per symbol, signed by side.
3. Compare median realized vs. the tier defaults in
   `SLIPPAGE_BPS_BY_TIER`. Replace the constants with empirical numbers,
   ideally per-symbol for the lowest-tier names where the variance is
   largest (HIMS, AFRM, RIVN, PINS, RBLX).
4. Once stable, wire `estimated_slippage_bps` into planner sizing —
   reduce `position_value` by `(slip_bps / 10000) × position_value`
   before the sub-min check. This will start dropping borderline
   sub-$1 intents that are net-negative after friction.

## 2. Cross-sleeve concentration cap

Status: NOT NEEDED on current numbers. Worst-case household concentration
in any single name is ~10% (Sonnet 12% × $1k + Opus 18% × $1k of $3k
total). Re-check if sleeve sizes diverge materially or per-name caps move.

## 3. Per-sector cap inside Sonnet

Status: deferred. 12-1 momentum is *supposed* to concentrate in the
trending sector — adding a sector cap dampens the alpha source. Revisit
only if a sector blow-up actually happens on paper data and Sonnet's
drawdown is materially worse than expected.

## 4. Defensive uncorrelated names for Haiku

Status: deferred. TLT/IEF/GLD already provide ballast. Adding DBMF/SVOL
is strategy expansion, not a fix. Revisit only if Haiku's downside is
worse than Faber-baseline benchmarks would predict.

## 5. Manager reallocation cadence

Status: monitor only. The 4-week cadence + ±25% step cap is appropriate
noise rejection. The startup log added in `app.py:start()` makes empty
sleeve_weights visible — if the file is still `{}` after the first
ISO-week-mod-4 Friday with the system running, debug the reallocation
job. Until then, `{}` (= base 1.0× per sleeve) is the correct default.

## 6. Force_close persistence

Status: largely vestigial after Bug #2 fix (live + replay paths now
share `_is_fully_filled` tolerance). The in-memory `qty` mutation in
`OMS.force_close_filled` is no longer load-bearing on next-boot recovery.
Safe to delete the qty-mutation line, but not urgent — keep until
post-paper-trade cleanup.
