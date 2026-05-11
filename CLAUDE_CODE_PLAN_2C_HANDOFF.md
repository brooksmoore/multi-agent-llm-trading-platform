# Plan 2c implementation — reshape the four-agent paper-trading bot for $0.10/day max-EV operation

## Repo

`/Users/brooksmoore/Desktop/Multi_Agent_Asset_Competitive_Bot`. Python 3.12, uv-managed. Read `logs/m9_complete.md` and `logs/m1_m8_audit.md` for prior context before changing anything.

## Current cost shape (verify before changing)

- `data/daily_spend.json` shows ~$0.01/day actual spend on a $0.95/day cap.
- `logs/app.log` shows `cached=0` on every Haiku call. Cause: `agents/prompts/haiku_agent.md` is ~1,440 tokens; `claude-haiku-4-5`'s minimum cacheable prefix is 2,048. Cache never writes.
- 66% of historical LLM calls return empty intents (see `logs/llm_responses.jsonl`).
- `Manager.risk_check`, `Manager.adversarial_critique`, and `Manager.master_capability_proposal` exist in `agents/manager_agent.py` but no scheduler/subscriber fires them.

## Goal: $0.10/day budget, shape:

| Slot | $/day | Notes |
|---|---|---|
| Cache-fixed sleeve loop (Haiku 6× + Sonnet 1× + Opus 1×/wk) | 0.030 | Same agents, cache-fixed, slimmer schedule |
| News-impact Haiku scoring per fresh item | 0.025 | New arm — fresh information |
| Haiku daily morning synthesis (replaces Manager morning_brief) | 0.005 | New "junior Manager" role on cheap model |
| Opus-Manager Friday weekly synthesis | 0.014 | Existing, amortized |
| Opus-Manager risk_check on extreme intents | 0.015 | Wires dormant code with tight threshold + rate-limit |
| Headroom for events (drawdown, MC proposal, surprises) | 0.011 | Real, not reserved |

Competitive-sleeve architecture preserved: each sleeve keeps its own lot ledger, drawdown bucket, capital allocation. Manager is overseer, not executor.

## Constraints

- Work on a new branch `plan-2c` off `master`. Do not commit the existing untracked `data/lots.db.bak-*` and `data/oms.db.bak-*` files (leftover from prior fix work).
- `alpaca_paper=False` must remain refused at `app.py:_build_alpaca_broker`.
- Dashboard stays bound to `127.0.0.1`.
- All existing tests must still pass. Add tests for every behavioral change.
- One commit per Tier-1 item, one commit per Tier-2 item. Clear messages. Exception: T1.6 lands in the same commit as T2.3 (see T1.6 note).
- If you hit ambiguity, stop and ask Brooks rather than guess. The audit log (`logs/m1_m8_audit.md`) shows prior agents quietly deferred ambiguities and accumulated debt — don't repeat that pattern.
- Do not amend prior commits. Do not skip hooks.
- When finished with each tier, run the full verification block (pytest + ruff + mypy on changed files) before starting the next tier.

## Tier 1 — free wins (land first, in this order)

### T1.1 — Fix the cache prefix

- Pad `agents/prompts/haiku_agent.md` above 2,048 tokens with stable content: more worked intent examples (right vs. wrong JSON), expanded counter-example section, exhaustive policy edge cases. Keep all dynamic placeholders (`{{...}}`) since `render_system_prompt` resolves them deterministically to `"(see context block)"`.
- Verify Sonnet/Opus prompts are above 1,024 tokens (their cache minimum). If not, pad similarly.
- Add `tests/test_llm_cache.py`: two Haiku calls 30s apart with identical system prompts; assert second call's `usage.cache_read_input_tokens > 0`. Requires real `ANTHROPIC_API_KEY` — mark with `@pytest.mark.integration` and skip cleanly when env var absent. CI doesn't need to run it; one local run suffices to verify.

### T1.2 — Reduce Sonnet schedule to 1×/day

- In `app.py`, remove jobs `JOB_SONNET_PRE_OPEN`, `JOB_SONNET_MID_MORNING`, `JOB_SONNET_MIDDAY`, `JOB_SONNET_POWER_HOUR`. Keep `JOB_SONNET_EOD` at 16:35 ET.
- Reason: 12-1 momentum on daily bars cannot change intraday.
- Update any scheduling test asserting a specific job count.

### T1.3 — Cut Opus daily check; weekly deep-dive only

- Remove `JOB_OPUS_DAILY`. Keep one of `JOB_OPUS_THURSDAY_DEEPDIVE` or `JOB_OPUS_FRIDAY_DEEPDIVE` (recommend Thursday — gives Friday's manager journal fresher input).
- Update scheduling test.

### T1.4 — Add `signal_fingerprint` to Opus and Manager

- `agents/opus_agent.py`: implement `signal_fingerprint(state)` hashing (holdings + watchlist + EMG + manager_directive). Return `None` during initiation mode (holdings < TARGET_HOLDINGS) so initiation-mode dives always run.
- `agents/manager_agent.py`: Manager doesn't extend `BaseAgent`. Instead, add a fingerprint helper used by `app.py` before calling Manager. Hash (VIX bucket + portfolio equity + each sleeve's drawdown bucket).
  - **Scope:** fingerprint applies only to `regime_read` and `weekly_journal` (the periodic strategic calls). Event-driven calls — `risk_check`, `drawdown_response`, `adversarial_critique`, `master_capability_proposal`, `capital_reallocation` — always run when their trigger fires.
- Test: two consecutive calls with identical state → second skipped.

### T1.5 — Build per-sleeve P&L attribution

- New `ops/attribution.py`: define a `PnLBreakdown` dataclass (`realized: Decimal`, `unrealized: Decimal`, `total: Decimal`, `num_open_lots: int`, `num_closed_lots: int`). Function `compute_daily_pnl(lots: LotLedger, oms_store: OMSStore, market_data: MarketData) -> dict[AgentId, PnLBreakdown]`. Use latest bar close for mark-to-market on open lots.
- New table `agent_pnl_daily(date TEXT, agent_id TEXT, realized DECIMAL, unrealized DECIMAL, total DECIMAL, num_open INTEGER, num_closed INTEGER)` in `data/equity_snapshots.db`. PRIMARY KEY (date, agent_id). Daily 16:45 ET job writes per-agent snapshot.
- Surface in a new dashboard panel (don't crowd the existing top strip) and a log line at write time.
- Add `tests/test_attribution.py`: seeded fills → assert per-agent realized P&L sums match expected; assert unrealized uses latest bar.

### T1.6 — Merge daily morning_brief into weekly regime_read

- **DEFERRED — pair with T2.3.** Removing `JOB_MANAGER_MORNING_BRIEF` before T2.3's `HaikuSynthesizer` lands would leave sleeves without a morning brief in the interval.
- When T2.3 lands: in the same commit, remove `JOB_MANAGER_MORNING_BRIEF` from `app.py` and add the new `JOB_HAIKU_MORNING_SYNTHESIS`. Keep `JOB_MANAGER_FRIDAY` (regime + journal + 4-weekly reallocation).

## Tier 2 — load-bearing additions

### T2.1 — Wire `Manager.risk_check` on high-conviction intents

- **Read `agents/prompts/manager_agent.md` for the `risk_check.json` schema before wiring.** Honor `verdict ∈ {approve, veto, resize}` and the `resized_target_weight` field if verdict=resize.
- In `App._submit_one_intent`, after RiskGate approval but before planner:
  ```python
  if intent.conviction >= 9 and intent.target_weight >= Decimal("0.08"):
      if self._risk_check_count_today() < 2:  # daily rate-limit
          manager_review = self.manager.risk_check(state, intent, ctx=...)
          verdict = manager_review.get("verdict")
          if verdict == "veto":
              self.outcome_recorder.record(
                  intent.id, intent.agent_id, "vetoed:manager_risk_check"
              )
              return False
          if verdict == "resize" and manager_review.get("resized_target_weight") is not None:
              intent = replace(
                  intent,
                  target_weight=Decimal(str(manager_review["resized_target_weight"])),
              )
      else:
          log.info("risk_check.skipped:rate_limit for intent %s", intent.id)
  ```
- **Rate-limit rationale:** Manager-on-Opus risk_check is ~$0.15-0.20/call; $0.015/day allocated allows ~1-2 fires/day. If the daily count is reached, log and allow the intent through unfiltered — the threshold itself is conservative, so this is acceptable.
- The `_risk_check_count_today()` helper queries `BudgetLedger` for today's entries with `call_type == "risk_check"`.
- Test: mock Manager returns `{"verdict": "veto"}`; assert no order submitted, outcome recorded. Test rate-limit by simulating 3 high-conviction intents in one day; assert third bypasses Manager and submits.

### T2.2 — News-impact scoring (Haiku)

- **First, back up the news DB:** `cp data/news.db data/news.db.bak-pre-2c-T2.2` before running the migration. Schema migrations are one-way; backup is cheap insurance.
- New `agents/news_scorer.py`: `NewsScorer(llm: LLMClient, store: NewsStore)`. Method `score(item: NewsItem) -> dict` returning `{impact: 1-5, affected_symbols: [...], surprise: "low|med|high"}`. Pre-filter: only score items with `body` non-None AND `len(body) > 200` AND at least one symbol in `PLUMBING_UNIVERSE`.
- **System prompt requirement:** NewsScorer's system prompt must be padded above 2,048 tokens (same as Haiku trend prompt) and reused across every call. Each unique system prompt is its own cache entry; cache hits start at the second item in each fetch batch, so amortization improves with batch size. Verify via `cached=N>0` in app.log on second-and-later items per batch.
- Schema: add `impact INTEGER`, `affected_symbols TEXT`, `surprise TEXT` columns to `news_items` table. Migration script in `scripts/migrate_news_schema_v2.py`. Idempotent (use `ALTER TABLE ... ADD COLUMN` wrapped in try/except for "duplicate column").
- `app.py`: after `news_fetcher.fetch_*` returns new items, iterate and run `NewsScorer.score(item)`. Publish `NewsHighImpactScoredEvent` (define in `core/events.py`) for items with `impact >= 4`. Event carries: `symbol`, `impact`, `headline`, `published_at`.
- Test: mock LLM response; assert row updated; assert event fired on impact=4 and not on impact=3.

### T2.3 — Haiku daily morning synthesis (junior Manager)

- New `agents/haiku_synthesizer.py`: `HaikuSynthesizer(llm: LLMClient, memory: AgentMemory)`. Reads positions across sleeves, last week's per-agent P&L (T1.5 output via direct SQLite query against `agent_pnl_daily`), top-5 scored news from prior 18h with impact ≥ 3 (T2.2), current VIX bucket.
- New call_type `morning_synthesis`. **System prompt must be padded above 2,048 tokens for cache reuse.** Produces ~200-word brief.
- Writes via `manager_bridge.write_morning_brief` so all sleeves see it on their next `observe()` via `AgentState.manager_morning_brief`.
- `app.py`: new job `JOB_HAIKU_MORNING_SYNTHESIS` at 08:30 ET Mon-Fri. **In the same commit, remove `JOB_MANAGER_MORNING_BRIEF`** (this closes T1.6).
- Test: seed inputs; mock LLM; assert non-empty brief persisted and readable by `manager_bridge.read_manager_context`.

### T2.4 — Wire `Manager.adversarial_critique` weekly

- `app.py`: new job Sunday 18:00 ET. Read prior week's 3 worst-realized-P&L intents per sleeve (join `agent_pnl_daily` + OMS store for intent-level realized P&L). Call `manager.adversarial_critique(state, intents, ctx)`. Persist via `manager_bridge.write_critique` per sleeve (one critique entry per affected sleeve).
- Test: seed prior-week intents; mock LLM; assert critique persisted and readable.

### T2.5 — Event-driven triggers

- Subscribe to `NewsHighImpactScoredEvent` (T2.2). On fire: if affected symbol is held by Opus, queue off-schedule Opus deep-dive on that symbol. **Rate-limit: max 1 extra Opus deep-dive per ISO week.** Track via opus memory key `extra_dives_iso_week_{YYYY-Www}`; reset implicit on key change.
- Add `PositionIntradayShockEvent` to `core/events.py`: fields `symbol: str`, `prev_close: Decimal`, `current_price: Decimal`, `shock_pct: Decimal`, `agent_holders: list[AgentId]`. Update `_scan_volatility_once` in `app.py:1305` to compute >5% intraday move on held names and publish.

### T2.6 — Update budget cap

- `config/settings.py`: `daily_spend_cap: Decimal = Field(default=Decimal("0.10"))`.

## Verification (after implementation, end-to-end)

1. `pytest` — all tests pass.
2. `ruff check` — clean. `mypy --strict` on changed files — clean.
3. Start `python app.py` with a real Anthropic key for one full trading day (paper).
4. End-of-day checks:
   - `data/daily_spend.json` shows `total_usd < 0.12` (steady-state target $0.10 + ~$0.01-0.02 cache-write overhead on first call of each window).
   - At least one log line in `logs/app.log` shows `cached=N>0` (cache verified hitting on second-and-later calls within a 1h window).
   - `data/news.db` has ≥1 row with `impact` populated, OR a log line `news_scorer: no items met pre-filter criteria today`.
   - Manager memory shows a `morning_brief` written by `HaikuSynthesizer` for today's date.
   - `agent_pnl_daily` table has one row per sleeve for today's date.
   - If any conv≥9 + target_weight≥8% intent fired AND rate-limit wasn't exhausted: a `manager/risk_check` entry exists in `daily_spend.json`.
5. Write `logs/plan_2c_complete.md` with observed daily spend breakdown vs. target allocation, any deviations explained, and any open questions for Brooks.

## Out of scope (do not do)

- Switching Manager from Opus to Sonnet model. (Considered, rejected — keep Opus brain on strategic calls.)
- Rules-only baseline / backtest engine. (Replaced by per-sleeve attribution.)
- Adversarial twin on Opus deep-dives. (Rejected as too conservative for the aggressive-growth thesis.)
- Anomaly scan, catalyst radar, watchlist pre-screen, calibration feedback loop. (Tier 3 — out of $0.10 budget.)
- Refactoring the two `AgentState` classes (`agents.base.AgentState` vs `core.types.AgentState`). (Pre-existing M9 backlog item, not in scope here.)
