# LEDGER — Multi_Agent_Asset_Competitive_Bot

> Append-only. Each audit session adds one block. Format: auditor · date · scope · findings (🔴 open / ✅ verified / 📝 note).
> See WORKFLOW.md §2 for the handoff relay this ledger supports.

---

## Audit 001 — 2026-06-07 · Claude (Sonnet 4.6) · Scope: GROK_HANDOFF_BACKTEST_HARNESS.md

**Work audited:** Grok's backtest harness session — `sonnet_momentum_weights`, `_price_momentum`, `deflated_sharpe`, `run_walk_forward`, CL-1 gate test in `test_sizing.py`, two new backtest tests.

**Files touched:** `backtest/strategies.py`, `backtest/engine.py`, `tests/test_backtest.py`, `tests/test_sizing.py`.

**Reproduce:** `.venv/bin/python -m pytest tests/test_backtest.py tests/test_sizing.py -v` → 37/37 green (independently confirmed).

---

### ✅ Verified

**✅ V-1: `_price_momentum` is bit-for-bit identical to `agents/sonnet_agent._price_momentum`.**
Compared `backtest/strategies.py:82-91` vs `agents/sonnet_agent.py:38-47`. Same guard (`required = lookback + skip`), same index arithmetic (`closes[-(lookback + skip)]`, `closes[-skip]`), same zero-division guard, same return formula (`exit_ / entry - 1`). No divergence.

**✅ V-2: Constants match live agent.**
Baseline `_SONNET_MOM_LOOKBACK=252`, `_SONNET_MOM_SKIP=21` vs live `_MOMENTUM_LOOKBACK=252`, `_MOMENTUM_SKIP=21`. Both frozensets import `SONNET_EQUITY_UNIVERSE + GROWTH_SLICE_UNIVERSE` from `config.universes` directly — same source.

**✅ V-3: Ranking invariant test is sound.**
Synthetic data construction verified: `strong_mom` series (rising, mom≈+0.245) wins `top_n=1`; `weak_mom` (falling, mom≈-0.025) is excluded. Flat filler names get mom=0.0 and are also excluded. Test logic is correct.

**✅ V-4: CL-1 — conviction never reaches the sizer.**
Independent code trace:
- `vol_targeted_position_value` signature: `(target_weight, agent_equity, realized_vol_annual, effective_vol_target)` — no `conviction`.
- `effective_max_gross` signature: `(agent_id, master_capability, vix_bucket, drawdown_bucket)` — no `conviction`.
- `planner.py:180`: passes only `target_weight=intent.target_weight` — `intent.conviction` is consumed only by `memory.record_intent()` (calibration log).
- Opus uses conviction as a **threshold filter** (skip if < 7), not a sizing multiplier — this is NOT Kelly-on-confidence; CL-1 still holds.
- Signature-inspection test passes. ✅

**✅ V-5: 37 tests green, no regressions.**
Full run of touched modules clean. Engine, strategies, sizing modules all import and execute without error.

---

### 🔴 Open

**🔴 OPEN-1 (HIGH): `run_walk_forward` is NOT a temporal split — DoD claim is premature.**
`run_walk_forward` re-runs the **full dataset** N times with linearly increasing `cost_bps`. This is a cost-sensitivity sweep, not a walk-forward cross-validation. A real walk-forward requires: (a) slice bars into sequential time windows, (b) hold out later windows as test sets, (c) fit/evaluate on earlier windows only, (d) aggregate excess CAGR and deflated Sharpe across folds. None of that is present. The DoD item reads "walk-forward CV + deflated Sharpe" — the current scaffold does not satisfy it. **No DoD box may be checked until real date-sliced windows are implemented.**
- The code comment acknowledges this ("A true split would require date filtering the input bars per window") — so Grok knew but shipped it anyway. Acceptable as a scaffold; NOT acceptable if called "done."

**🔴 OPEN-2 (MEDIUM): `_SONNET_TRADABLE` duplicated — DRY violation, latent parity risk.**
`_SONNET_TRADABLE` is constructed identically in both `agents/sonnet_agent.py:18-20` and `backtest/strategies.py:29-31`. Currently no parity gap (both derive from `config.universes`). But any direct edit to one frozenset silently diverges from the other, defeating the "identical signal" guarantee that is the entire point of the rules-only baseline. Fix: baseline should import `_SONNET_TRADABLE` directly from `agents.sonnet_agent`, or both should be exported from `config.universes`.

**🔴 OPEN-3 (HIGH): Fail-before sequence is unverifiable — no intermediate git commit.**
Grok claims strict fail-before TDD (test written first → ImportError → implementation → green). But all changes landed in the working tree together; there is no git commit showing the test failing before the implementation. The fail-before standard requires a git commit sequence: commit A (test only, known-failing), commit B (implementation, now green). Future builder sessions MUST provide this git evidence. Assertion of fail-before without git proof does not satisfy the standard.

**🔴 OPEN-4 (HIGH): CL-1 gate lives in a builder-editable file — not auditor-owned.**
`test_sizing_functions_never_see_llm_conviction_scalar` is in `tests/test_sizing.py`, which Grok can freely modify. WORKFLOW.md §6 and CL-2 both mandate auditor-owned `test_audit_*_gate.py` files the builder may NOT edit. **Auditor action required:** extract this test (and future invariant gates) into `tests/test_audit_sizing_gate.py` and document it as builder-hands-off. This file must not be created by Grok.

---

### 📝 Notes (not blocking, informational)

**📝 N-1: `deflated_sharpe` is a placeholder, not the Bailey/Lopez de Prado formula.**
Current implementation uses a rough `sqrt(2*ln(N)) / sqrt(T)` penalty. Handoff correctly acknowledges this. Fine as an extension point but the DoD milestone requires the full formula (skew, kurtosis, # trials) before a deflated SR number can be quoted as evidence.

**📝 N-2: Flat-series names get mom=0.0 in the ranking test, not None.**
With n=300 bars and lookback+skip=273, flat fillers have mom=0.0 (valid, non-None). They are added as candidates with zero momentum. Top-N selection still works correctly because AAPL dominates. Not a bug, but worth knowing when reading test output.

---

*Handoff written: GROK_HANDOFF_BACKTEST_HARNESS.md overwritten with numbered fix list for Grok.*

---

## Audit 002 — 2026-06-09 · Claude · Scope: Grok's response to Audit 001 (OPEN-1 + OPEN-2)

**Work audited:** Grok's fixes for walk-forward temporal splits (OPEN-1) and `_SONNET_TRADABLE` deduplication (OPEN-2). Claimed commits: c221efb / 89b0a65 (Fix 1), 564fae2 / 7b62629 (Fix 2).

**Reproduce:** `.venv/bin/python -m pytest tests/test_backtest.py tests/test_sizing.py tests/test_audit_sizing_gate.py -q` → 40/40 green (independently confirmed).

---

### ✅ Verified

**✅ V-1: RED→GREEN commit sequence exists and is genuine (OPEN-3 resolved for these fixes).**
`git log --oneline` confirms all 4 commits in order: c221efb (RED test), 89b0a65 (GREEN impl), 564fae2 (RED test), 7b62629 (GREEN impl). Commit SHAs match claims in handoff exactly.

**✅ V-2: `run_walk_forward` now performs real temporal date slicing (OPEN-1 resolved).**
Code inspection of `backtest/engine.py:228-302`:
- Builds master calendar from benchmark symbol (same as `run_backtest`).
- Splits calendar into `n_windows` sequential non-overlapping chunks.
- Each fold filters `bars_by_symbol` to only dates within `[win_start, win_end]`.
- Embargo logic present (shifts next-window start by `embargo_days`).
- `test_walk_forward_uses_distinct_date_windows` asserts `results[i].dates[-1] < results[i+1].dates[0]` — passes.
- The old smoke test (`test_walk_forward_and_deflated_sharpe_smoke`) continues to pass on the new implementation.

**✅ V-3: `_SONNET_TRADABLE` imported directly from live agent (OPEN-2 resolved).**
`backtest/strategies.py:25` is now `from agents.sonnet_agent import _SONNET_TRADABLE`. Parity is now structural (import = same object), not coincidental (two frozensets from same source). Identity test passes.

**✅ V-4: Builder respected auditor-owned gate (Fix 4 / OPEN-4).**
Grok did not create or edit `tests/test_audit_sizing_gate.py`. The file is intact and unchanged from auditor's version.

**✅ V-5: 40/40 tests green — no regressions.**

---

### 🔴 Open

*(None from this audit. All four Audit 001 open items are now resolved.)*

---

### 📝 Notes

**📝 N-1: Walk-forward is still not a full purged/embargoed CV with per-fold hyperparam search.**
The implementation satisfies the DoD "walk-forward CV" structural requirement (non-overlapping forward folds). The handoff correctly notes the remaining gap: to quote the DoD graduation criteria, the *verdict* from walk-forward ("fraction of windows where rules baseline excess > 0 and deflated SR > X") still requires running real yfinance data through the harness. The harness is now correctly built; the data run is the remaining step.

**📝 N-2: GROK_HANDOFF_BACKTEST_HARNESS thread is now audit-clean. Per WORKFLOW.md §2 DONE_ convention, this thread is eligible to be renamed `DONE_GROK_HANDOFF_BACKTEST_HARNESS.md`. Auditor renamed accordingly — see next line.**

*Thread renamed to DONE_GROK_HANDOFF_BACKTEST_HARNESS.md per WORKFLOW.md §2.*

---

## Build 003 — 2026-06-13 · Claude (Opus 4.8, builder-on-Grok's-behalf) · Scope: Robinhood live-broker auth + response parsing

**Work:** Solved Robinhood agentic MCP authentication (multi-hour roadblock) and corrected the broker's response parsing against verified live tool shapes.

**Files touched:** `execution/robinhood_broker.py`, `app.py`, `config/settings.py`, `tests/test_robinhood_broker.py`, `tests/test_smoke.py`, `scripts/robinhood_mcp_connect.py` (new), `.gitignore`, `pyproject.toml`/`uv.lock` (+mcp). Deleted: `execution/robinhood_token.py`, `scripts/robinhood_oauth.py`, `scripts/robinhood_probe.py`.

**Reproduce:** `uv run pytest -q` → 761 passing.

---

### ✅ Verified

**✅ V-1: Auth root-caused + fixed via official MCP SDK.**
Hand-rolled PKCE script could never trigger Robinhood's consent screen — it skipped the MCP `401 → /.well-known discovery → dynamic register → authorize` handshake, so `robinhood.com/oauth` just rendered the Agentic dashboard (logged-in, incognito, and post-disconnect all identical). Switched transport to the `mcp` Python SDK (`OAuthClientProvider` + `streamablehttp_client`). One-time `scripts/robinhood_mcp_connect.py` ran the flow → consent appeared → authorised. Token persisted to `~/.robinhood_mcp_tokens.json` (access ~4d + long-lived refresh).

**✅ V-2: Headless autonomy confirmed.** Reconnect with no browser + SDK auto-refresh verified. `_McpSdkClient` bridges the async SDK into the sync `Broker` via a daemon event loop; runtime redirect/callback handlers raise `BrokerError` so it never opens a browser unattended. Live read-only `get_account`/`list_positions` exercised end-to-end through real broker code.

**✅ V-3: Response-envelope + field bugs fixed against LIVE shapes.** Every RH tool wraps payload in `data` (not `results`). Fixed `get_account` (→ `get_portfolio`: `total_value`/`cash`/nested `buying_power.buying_power`), `list_positions` (`data.positions`), `get_order` + `find_order_by_client_id` (`data.orders`). `_translate_order` filled-qty now reads `cumulative_quantity` (was `filled_quantity`, which does not exist — would have made every fill invisible to the Reconciler on real money) and timestamp `last_transaction_at`. 4 new offline tests with canned real-shape responses.

---

### 🔴 Open

**🔴 O-1 (task_4baf2497 follow-on): `BrokerPosition.current_price` = 0.** `get_equity_positions` carries no market price; live valuation/PnL needs `get_equity_quotes`. Affects dashboard valuation, NOT reconciliation (qty is correct). `TODO` marked in `_translate_position`.

---

### 📝 Notes

**📝 N-1: Agentic account is a CASH account** (981398050) — no PDT concept, so `pattern_day_trader=False`/`daytrade_count=0` are correct constants, not placeholders. Personal margin account 891728651 must never receive bot orders.

**📝 N-2: Shared generic OAuth client** ("Robinhood Trading", one grant slot per account). If the user reconnects Claude Desktop to RH it may contend for the grant — leave Claude Desktop disconnected so the bot owns it.

**📝 N-3: Still UNFUNDED.** All live calls returned zeros (accurate). Fund 981398050 before flipping `ROBINHOOD_LIVE_ENABLED=true`. Verify funded `get_portfolio` balances populate, then run a tiny live test.
