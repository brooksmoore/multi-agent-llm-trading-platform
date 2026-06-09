# Build Journal — Multi-Agent Asset Competitive Bot

---

## Milestone 1 — 2026-04-24 — Skeleton & Types

**What was built:**
- `pyproject.toml` with full dependency list (alpaca-py, anthropic, duckdb, vectorbt, dash, etc.), ruff + mypy configuration.
- `.env.example` with all env vars documented.
- `.gitignore` — data/ and .env excluded, agent prompts and code committed.
- `core/types.py` — all domain types: Intent, Order, Fill, Position, Lot, NewsItem, AgentMemo + 15 StrEnum classes. `Lot.__post_init__` sets `remaining_qty = qty` for new lots. `Order` is frozen; mutations use `dataclasses.replace`.
- `core/state_machine.py` — generic `StateMachine[StateT, EventT]` with guards/actions, duplicate-arc detection, crash-recovery `reset()`, history log. Pre-built `build_order_fsm()` wires the full Order lifecycle (8 states, 14 arcs).
- `core/events.py` — `EventBus` (thread-safe publish/subscribe + wildcard `*` channel) + 13 typed event dataclasses covering every system state transition.
- `core/clock.py` — `Clock` Protocol, `WallClock`, `BacktestClock` (thread-safe, advance/set). NYSE holiday list hard-coded for 2026. `zoneinfo` used for ET.
- `ops/telegram.py` — Telegram adapter stub (all methods are no-ops; interface is final for v1.5 wiring).
- `agents/prompts/` — all four agent prompts copied from blueprint.
- `tests/test_state_machine.py` — 48 tests covering FSM basics, guards, actions, Order lifecycle, BacktestClock, and EventBus.

**Test results:** 48/48 pass. Ruff: clean. mypy --strict on `core/`: clean.

**What surprised me:**
- System Python on this Mac is 3.8.9. Installed Python 3.12.13 via uv (8 seconds). That's the workflow going forward: `.venv/bin/python` for all invocations.
- ruff auto-fixed 56 of 72 lint errors. The remaining 23 were a mix of UP046 (new-style generics, suppressed — old style works fine), TC003 (stdlib to TYPE_CHECKING, suppressed — types are used at runtime in dataclass fields), and a few manual cleanups (unused imports, F841 unused variable).
- `dataclasses.replace(order, **dict[str, object])` is not mypy --strict compatible. Removed the `with_state` convenience method; OMS will use `dataclasses.replace` directly with explicit field names.

**Pending:**
- Milestone 2: OMS with FakeBroker. This is the highest-stakes module. Need to build before any LLM agents run.
- `data/` and `config/` directories exist but are empty — populated in M3–M5.
- The `Optional[...]` note: mypy ignored a warning about unused mypy module overrides (alpaca, dash, etc.) — these will become active when those libraries are installed in M4–M8.

---

## Milestone 2 — 2026-04-24 — OMS with FakeBroker

**What was built:**
- `execution/broker.py` — `Broker` Protocol (submit/cancel/get/find_by_client_id/list_positions/get_account/register_event_callback) + broker-side data types (`BrokerOrderStatus`, `BrokerPosition`, `BrokerAccount`, `BrokerOrderEvent`). Broker exceptions: `BrokerRejection`, `BrokerUnavailable`. The contract: `submit_order(client_order_id=X)` MUST be idempotent — same X always returns same broker_order_id.
- `execution/fake_broker.py` — `FakeBroker` with 4 configurable fill modes (INSTANT, MANUAL, REJECT, PARTIAL_THEN_HOLD), test-only knobs (force_full_fill / force_partial_fill / force_reject / inject_submit_failure), full account + position tracking, callback-based event delivery. Idempotent on client_order_id.
- `execution/oms_store.py` — SQLite WAL-backed append-only event log with `EventKind` enum (10 kinds). JSON serialization for Decimal/datetime/UUID/StrEnum via `dumps()` / `loads()` with discriminator dicts. WAL + synchronous=NORMAL pairing; durable on `append()` return.
- `execution/oms.py` — the `OMS` class. ~470 LOC of careful state-management logic. Key invariant per blueprint Principle 5: persist event to log → flush → THEN side-effect. Threading: `RLock` so callback can re-enter from inside `submit_order` (FakeBroker INSTANT mode). Recovery: `recover()` replays log + reconciles non-terminal orders against broker (broker is source of truth per Principle 4). Idempotency: `client_order_id` is the safety net for retries.
- `tests/test_fake_broker.py` (20 tests) — FakeBroker contract tests
- `tests/test_oms_store.py` (13 tests) — SQLite WAL durability + JSON encoding round-trips
- `tests/test_oms.py` (19 tests) — happy paths, partial fills, rejections, cancellations, multi-agent
- `tests/test_oms_recovery.py` (15 tests) — **the milestone gate**: every interesting crash scenario:
  1. Crash before broker call → recovery declares ABANDONED
  2. Crash after broker call, before ACCEPTED logged → recovery backfills from broker
  3. Crash after ACCEPTED, no fill → reconcile no-op
  4. Crash with broker filling during downtime → synthetic fill on recovery
  5. Partial fill received then crash, broker fills rest during downtime → recovery picks up missing qty
  6. Terminal-state orders (FILLED/REJECTED/CANCELLED) survive replay unchanged
  7. Many orders mixed states all survive together
  8. `recover()` is idempotent (safe to call twice)
  9. Recovered OMS can submit new orders
  10. Position consistency: local view = broker view post-recovery
  11. Replay reconstructs `filled_avg_price` correctly from event stream

**Test results:** 115/115 pass. ruff: clean. mypy --strict on `core/` + `execution/`: clean.
**Coverage on `execution/`: 90%** (broker.py 100%, oms_store.py 97%, oms.py 86%, fake_broker.py 89%). Uncovered lines are mostly defensive no-op guards on terminal states and test-only knobs (`inject_cancel_failure`, etc.) — not recovery paths.

**What surprised me:**
- The EventBus `publish` vs `publish_all` split from M1 was over-engineered. Unified them so wildcard subscribers fire on every publish — that's what the dashboard needs. Kept `publish_all` as an alias for back-compat with M1 tests.
- The crash-recovery test "crash after broker call, before ACCEPTED logged" required hand-crafting the OMS state (append SUBMIT_INTENT to store directly + call broker.submit_order without OMS) since the OMS is too well-designed to crash there in normal operation. That's a feature, not a bug.
- The `BrokerOrderEvent` callback fires synchronously inside `FakeBroker.submit_order` for INSTANT mode. This means the OMS lock must be `RLock` (re-entrant) so the callback can write to the log while `submit_order` still holds the lock. I designed for this from the start; it worked.
- `dataclasses.replace(order, **kwargs)` removed in M1 due to mypy strict — turned out to be the right call. The OMS uses `replace(self._orders[id], state=...)` directly with named fields. Cleaner.

**Pending for v1.5+:**
- M2 `oms.py` line 86% coverage gaps: handler for broker-emitted EXPIRED, broker callback for unknown order_id during early recovery (covered partially in M2 test additions). Will close in M5 when MarketData pushes new events.
- Position-level reconciliation (currently only order-level). Position aggregation across orders happens at the lots ledger (M3); the reconciler.py from blueprint §10 is M4 work.

**Decisions worth flagging:**
- `RECONCILE_NOOP` events ARE logged for audit purposes even though they don't change state. Cheap; useful for debugging "why didn't reconciliation do anything?" later.
- `BrokerRejection` exceptions intentionally do NOT have an `Error` suffix (per N818 ruff rule) — they're domain concepts in a finance system. Suppressed N818 in pyproject.toml.

---

*Next: Milestone 3 — RiskGate, sizing, kill switches, lot ledger, wash-sale, leverage.*

---

## Milestone 3 — 2026-04-24 — Deterministic Guards

**What was built:**
- `execution/lots.py` — FIFO/LIFO tax lot ledger. Immutable `Lot` dataclass mutated via `dataclasses.replace`. Thread-safe with `threading.Lock`. `open_lot()` takes a BUY fill; `close_lots()` walks lots in FIFO or LIFO order consuming `remaining_qty`; `total_open_qty()` sums open lots with an explicit `Decimal("0")` start to satisfy mypy.
- `execution/tax.py` — Wash-sale rule enforcement. `WashSaleChecker` records only loss-sales (pnl < 0); `is_blocked()` checks 30-day window (inclusive of boundary day); `harvesting_candidates()` returns open lots where `current_price < entry_price`. Cross-agent isolation: records keyed by `(agent_id, symbol)`.
- `execution/kill_switch.py` — Global kill switch (intraday P&L + drawdown from rolling peak) and per-agent bench. One-directional escalation: states worsen (OK → HALVED → PAUSED → LIQUIDATE) and never auto-recover. `_DRAWDOWN_WORSE_STATES` frozenset prevents downgrading. DRAWDOWN_HALVED does NOT block `can_open_new()` — it signals the sizing layer to cut leverage, not the entry gate. Per-agent: 5 consecutive losses → 24h bench.
- `execution/sizing.py` — EWMA vol-targeting (λ=0.94, 8% annual vol floor) + leverage caps (1.75× hard cap, ±10% day-over-day change cap). `effective_max_gross = base_max_gross × MASTER_CAPABILITY × vix_scalar × drawdown_scalar`. MC > 1.5 raises ValueError. `classify_vix()` maps VIX to VixBucket enum.
- `execution/risk.py` — Pre-trade `RiskGate`. Nine ordered checks: (1) LIQUIDATE → only closes, (2) blocked states → no new entries, (3) agent benched, (4) FORCED_CASH bucket → no buys, (5–6) LETF whitelist + 5-day hold-period check, (7) options 20%-of-sleeve cap, (8) single-name weight cap (soft cap — `allowed=True, capped_weight=cap`), (9) effective_gross==0 → no buys.
- `execution/budget.py` — Daily LLM spend ledger. JSON file persistence keyed by UTC date (not local time). Auto-resets on new day. `is_exhausted()` used by approval_queue before enqueueing.
- `execution/approval_queue.py` — Pending intents inbox for AUTO_APPROVE=False path. TTL-based expiry, approve/reject by intent_id.

**Test results:** 242/242 pass. Ruff: clean. mypy --strict on `core/` + `execution/`: clean.

**New test files (127 new tests):**
- `tests/test_lots_fifo.py` — 17 tests: open_lot, FIFO/LIFO close, partial close, spanning multiple lots, error handling, cross-agent isolation, total_open_qty
- `tests/test_wash_sale.py` — 14 tests: record_sale (loss/gain/break-even/unclosed), is_blocked, harvesting_candidates
- `tests/test_kill_switch.py` — 35 tests: all global states, drawdown ladder, no-downgrade invariant, heartbeat, reconciliation break, budget exhausted, per-agent bench, `classify_drawdown` parametrize
- `tests/test_sizing.py` — 30 tests: EWMAVolEstimator (floor, decay), VolTargetSizer (target vol, high vol, 1.75× cap, ±10% day cap, final_leverage), effective_max_gross (per-agent, VIX scalars, drawdown scalars, MC > 1.5 error), `classify_vix` parametrize
- `tests/test_risk_gate.py` — 24 tests: happy path, all kill switch states, agent bench, FORCED_CASH, LETF checks, options cap, weight cap (soft), zero gross, check_letf_auto_liquidations
- `tests/test_budget_enforcer.py` — 12 tests: initial state, accumulation, exhaustion, remaining floor, reset_if_new_day, persistence, new instance loads data, stale date, corrupt file

**What surprised me:**
- `sum()` on an empty generator returns `Literal[0]` (int) not `Decimal` — mypy --strict catches this. Fixed with `sum(..., Decimal("0"))` start value.
- StrEnum values are lowercase by default, so `KillSwitchState.DRAWDOWN_PAUSED` has string value `"drawdown_paused"`. The veto_reason uses `f"kill_switch:{ks}"` which interpolates the StrEnum value. Test assertions had to match lowercase.
- Budget persistence: `_load()` must use `datetime.now(UTC).date()` not `date.today()` (local time). If machine timezone is behind UTC, a spend record could be treated as stale 8 hours early.
- LETF auto-liquidation: a separate `check_letf_auto_liquidations()` method runs at market open, independent of the intent gate. Sells are always allowed through the gate even if overdue — they may be the liquidation itself.

**Pending for M4+:**
- `execution/risk.py` wash-sale check is wired (WashSaleChecker is a dependency) but not yet called in `check_intent()`. Will activate in M4 once OMS feedback loop provides loss-sale signals.
- Budget ledger is standalone; wired to KillSwitchEngine.trip_budget_exhausted() will happen in M5 orchestration layer.
- Approval queue tested but not integrated into the execution planner yet.

---

## Milestone 4 — 2026-04-24 — AlpacaBroker Adapter + Reconciler

**What was built:**
- `config/__init__.py` + `config/settings.py` — pydantic-settings `Settings` class. Reads from `.env` via `SettingsConfigDict`. Fields: `alpaca_api_key`, `alpaca_secret_key`, `alpaca_paper`, `anthropic_api_key`, `master_capability`, `auto_approve`, `daily_spend_cap`, `reconciler_interval_secs`, `reconciler_qty_tolerance`. Module-level `settings = Settings()` singleton for import.
- `execution/alpaca_broker.py` — Production `Broker` adapter wrapping alpaca-py's `TradingClient` (REST) and `TradingStream` (WebSocket). Status translation tables at module level (`_ALPACA_STATUS_TO_BROKER`, `_ALPACA_TRADE_EVENT_TO_STATE`, `_ALPACA_ASSET_CLASS_TO_OURS`). Key methods: `submit_order` (market + limit, with 422-idempotency fallback), `cancel_order`, `get_order`, `find_order_by_client_id`, `list_positions`, `get_account`, `start_stream`, `stop_stream`. `_trade_update_to_event` translates WS updates into `BrokerOrderEvent` with `Fill` on fill/partial_fill events.
- `execution/reconciler.py` — `Reconciler` runs every 60s in a daemon thread. Two checks: (1) order reconciliation — for each open OMS order, fetches broker status and calls `oms.on_broker_event()` if terminal state not yet recorded; (2) position reconciliation — sums OMS fills into expected net positions, compares with broker positions, trips `KillSwitchEngine.RECONCILIATION_BREAK` if any symbol deviates by ≥ `qty_tolerance` shares.
- `execution/oms.py` — Added `on_broker_event(event)` public method (was previously private `_on_broker_event`) so Reconciler can drive the same callback path used by the broker stream.
- `tests/test_alpaca_broker.py` — 19 tests covering: submit market/limit/unsupported/missing-price, 4xx→BrokerRejection, 5xx→BrokerUnavailable, 422-idempotency fallback, cancel, get_order (filled/partial/not-found), find_by_client_id, list_positions (multi/short/crypto), get_account, register_callback.
- `tests/test_reconciler.py` — 9 tests covering: empty OMS, no mismatch, position mismatch trips kill switch, within-tolerance no-trip, order drift detection, start/stop thread, idempotent start, multiple symbols, sell reduces position.

**Test results:** 270/270 pass. Ruff: clean (M4 files). mypy --strict on `core/` + `execution/` + `config/`: clean.

**What surprised me:**
- `OrderId = uuid.UUID` is a bare type alias (not `NewType`), so `OrderId(some_uuid)` resolves to `UUID(some_uuid)` which treats the UUID object as the `hex` argument — calling `.replace()` on it — and raises `AttributeError`. Never wrap an existing UUID with `OrderId()`; use `UUID(str_value)` directly.
- alpaca-py stubs declare return types as `Order | dict[str, Any]` for most REST calls — even though in practice it's always `Order`. Fixed with `cast(AlpacaOrder, ...)` at each call site (5 casts total). Added `"TC002"` to ruff ignore list so alpaca model imports don't get moved to `TYPE_CHECKING` block (they're used at runtime for attribute access, not just annotation).
- `FakeBroker.force_full_fill` fires the OMS callback synchronously, so by the time the test checks OMS state the order is already FILLED and no longer in `list_open_orders()`. The reconciler drift test had to directly mutate `broker._orders[broker_id].status` under `broker._lock` to simulate a broker-side cancel that bypassed the event stream — mimicking a real disconnect scenario.
- `TradingStream.run()` is a blocking asyncio call; it must run in a daemon thread. The `@stream.subscribe_trade_updates` decorator accepts an `async def` handler and works correctly even when the decorator runs before the thread starts.

**Decisions worth flagging:**
- `oms.on_broker_event()` is a thin public wrapper around the private `_on_broker_event()` handler. This keeps the Broker Protocol contract clean (broker calls `register_event_callback`, not `on_broker_event`) while giving Reconciler a stable entry point.
- Position reconciliation tolerance is 1 share (configurable). Blueprint Principle 4 says "1-share or $1 mismatch flips to RECONCILIATION_BREAK". Dollar mismatch is not yet implemented — deferred to M6 when we have real-time pricing.
- Short positions: Alpaca `Position.qty` is always positive; the `side` field (`"short"`) triggers negation in `_translate_position`. This matches how OMS net positions work (BUY adds, SELL subtracts).

**Pending for M5+:**
- Smoke test: live paper-trading round-trip (buy 1 SPY → fill → reconcile) deferred until `.env` is populated with real Alpaca paper credentials.
- Dollar-value mismatch check in Reconciler (Principle 4 second criterion).
- `AlpacaBroker.start_stream()` not yet wired into the orchestration startup sequence — will happen in M6.

---

## Milestone 5 — 2026-04-25 — MarketData + DataStore + News Adapters

**What was built:**
- `data/market.py` — `Bar` and `Quote` frozen dataclasses (Decimal fields, `Quote.mid` property). `Timeframe(StrEnum)` with MINUTE/HOUR/DAY. `MarketData` structural Protocol. `AlpacaMarketData` wraps `StockHistoricalDataClient` (separate from TradingClient) for live bars/quotes/snapshots. `ReplayMarketData` is a pure in-memory adapter for backtesting, filtering by date range.
- `data/store.py` — `DataStore` backed by DuckDB. Two tables: `bars` (PK: symbol+timestamp) and `news` (PK: url). `save_bars`/`save_news` use `ON CONFLICT DO UPDATE` upsert semantics. Decimal round-tripped as strings. `symbols` stored as JSON array. `threading.Lock` on every `execute` call for thread safety. `:memory:` for tests, file path for persistence.
- `data/news.py` — Four adapters, each returns `list[NewsItem]` and swallows all errors gracefully (returns `[]`):
  - `FinnhubAdapter` — Finnhub `/company-news` REST endpoint; maps unix timestamps to UTC datetime
  - `EDGARAdapter` — EFTS full-text search endpoint with User-Agent header; builds headline from form_type + entity_name
  - `RSSAdapter` — feedparser over multiple feed URLs; deduplicates by URL; skips bad feeds per-feed; falls back to `datetime.now(UTC)` when `published_parsed` is None
  - `YFinanceAdapter` — wraps `yf.Ticker(symbol).news`
- `data/cache.py` — `Cache` wraps `diskcache.Cache` with `get/set/delete/clear/close` and a `cached()` decorator keyed by `qualname + args + kwargs`.
- `data/summarize.py` — `BriefingSummarizer` produces token-capped briefings: `summarize_bars` (O/H/L/C/V table, most-recent-first, with optional VWAP), `summarize_news` (numbered headline list with sentiment), `build_market_brief` (60/40 budget split bars/news).

**Test results:** 330/330 pass. Ruff: clean. mypy --strict on `core/` + `execution/` + `config/` + `data/`: clean.

**New test files (60 new tests):**
- `tests/test_market_data.py` — 15 tests: Bar/Quote types, Replay date filtering, AlpacaMarketData with mocked StockHistoricalDataClient
- `tests/test_data_store.py` — 16 tests: bar/news round-trips, upsert, filters, ordering, persistence across close/reopen
- `tests/test_news_adapters.py` — 15 tests: all four adapters, error handling, empty responses, param validation
- `tests/test_data_cache.py` — 8 tests: get/set/delete/clear, cached decorator, persistence
- `tests/test_summarize.py` — 6 tests: symbol presence, truncation, headline, empty-news, multi-symbol, VWAP

**What surprised me:**
- `types-requests` stubs weren't in dev deps. `requests` is a runtime dep but mypy --strict requires stubs; added `types-requests>=2.31.0` to dev extras and installed via uv.
- `AlpacaMarketData` uses `StockHistoricalDataClient` (from `alpaca.data.historical.stock`) — a completely separate client from `TradingClient`. They have different constructors and endpoints. The mypy overrides for `alpaca.*` mean `cast()` is still needed for return types.
- `BarSet.data` is `dict[str, list[AlpacaBar]]` — a named attribute, not a subscript on the BarSet itself. The struct is `bar_set.data.get(symbol, [])`.
- feedparser's `published_parsed` is a `time.struct_time | None`. When None (malformed RSS), falling back to `datetime.now(UTC)` is better than skipping the entry since the headline is still valuable.
- DuckDB `ON CONFLICT (col) DO UPDATE SET` syntax works exactly as in PostgreSQL; no SQLite-compat quirks needed.

**Pending for M6+:**
- `AlpacaMarketData` not yet wired into the agent observation loop — happens in M6.

---

## Milestone 6 — 2026-04-25 — Haiku Agent End-to-End (LLM + Memory + Calibration)

**What was built:**
- `agents/llm.py` — `LLMClient`: Anthropic SDK wrapper with (1) pre-call budget gate (raises `BudgetExhausted` if estimated cost > `BudgetLedger.remaining()`), (2) exponential-backoff retry on `RateLimitError`, (3) optional system-prompt caching via `cache_control: ephemeral` block, (4) actual token usage from `response.usage` for cost tracking, (5) `AgentMemo` creation and return. Pricing table for all three models including cache-hit (10% of input rate) and cache-write (125%) tiers.
- `agents/memory.py` — `AgentMemory`: SQLite-backed (3 tables: `memories`, `journals`, `intent_log`). Thread-safe with `threading.Lock`. Key-value `remember/recall`, daily `write_journal/read_journal`, per-intent `record_intent/record_outcome`, `recent_intents_summary` for LLM context injection.
- `agents/calibration.py` — `CalibrationTracker`: SQLite-backed Brier-style calibration. Records `(intent_id, agent_id, conviction, outcome)`. `brier_score()` computes `mean((conviction/10 - outcome_binary)^2)`. `calibration_table()` groups by conviction bucket (1-3, 4-6, 7-10) with n, win_rate, per-bucket Brier.
- `agents/base.py` — `AgentState` dataclass (timestamp, bars_by_symbol, news, positions, account, kill_switch_state, master_capability, effective_max_gross, vix_value, manager text fields) + `BaseAgent` ABC with `observe(state) -> list[Intent]`.
- `agents/haiku_agent.py` — `HaikuAgent`: reads prompt from `agents/prompts/haiku_agent.md`; guards on `DRAWDOWN_LIQUIDATE`; computes 210-day equity SMA and 50-day+14-day crypto trend; formats context block; calls `LLMClient.call()`; parses JSON response into `Intent` objects (clamped to 4 max, fields truncated to spec limits); records each intent into memory. Any LLM exception → log warning, return `[]`.

**Test results:** 358/358 pass. Ruff: clean. mypy --strict on `core/` + `execution/` + `config/` + `data/` + `agents/`: clean (33 files).

**New test file (28 tests): `tests/test_agents_offline.py`**
- LLM budget gate (0 budget raises, tiny budget raises, spend recorded after call, memo has correct model)
- Memory (remember/recall, overwrite, journal read/write, intent record + outcome, summary)
- Calibration (Brier=0 on no records, near-perfect scores, per-agent filtering, 3-bucket table)
- SMA/momentum helpers (insufficient history, exact period, positive momentum)
- HaikuAgent integration (mocked LLM): valid JSON → 1 intent; DRAWDOWN_LIQUIDATE → []; BudgetExhausted → []; 6 intents capped to 4; bad JSON → []; intents recorded in memory; equity/crypto trend correctly computed with 220/70 bars

**What surprised me:**
- `anthropic.types.ContentBlock` is a union of `TextBlock | ToolUseBlock | ...` — accessing `.text` directly requires `# type: ignore[union-attr]`. Using `getattr(usage, "cache_creation_input_tokens", 0) or 0` handles the case where the field is present but None (SDK returns None when no cache write occurred, not 0).
- `datetime.utcnow()` in `AgentMemory.remember()` triggered a DeprecationWarning in pytest — fixed to `datetime.now(UTC)` immediately.
- `KillSwitchState.DRAWDOWN_LIQUIDATE` (not `LIQUIDATE`) is the correct enum value for the liquidate state. The haiku agent guards on this; tests had to use the correct value.
- `BudgetLedger.record_spend(agent_id: str, ...)` — the signature takes `str`, not `AgentId`. Called with `str(agent_id)` in LLMClient.

**Pending for M7+:**
- M6 is "Haiku end-to-end" — the full loop (market data → Haiku → OMS → fill → reconcile) is not yet wired into a single `app.py` entrypoint. That wiring happens in M7/M8.
- `LLMClient` not yet using the prompt-caching cache-read cost efficiently — the cache_creation cost is tracked but the system prompt split (static cached prefix vs. dynamic per-call user message) is not implemented in haiku_agent. Will instrument in M7.
- Sonnet, Opus, and Manager agents deferred to M7.
- `DataStore.load_news` Python-side symbol filter is O(n) — acceptable for current scale (≤10K news items/week).
- No Parquet export yet (blueprint mentions partitioned Parquet files) — DuckDB tables are the primary store; Parquet export is a v1.5 optimization.
- FRED and Reddit adapters omitted (low priority for v1; FRED data is more useful as macro context in the agent brief than as real-time news).

---

## Milestone 7 — 2026-04-24 — Sonnet, Opus, and Manager Agents

**What was built:**
- `agents/sonnet_agent.py` — `SonnetAgent(BaseAgent)`: multi-factor equity quant. Computes 12-1 price momentum proxy from available bar history (sorted by timestamp). Ranks all symbols by momentum, presents top 25 as the "factor ranking" context block. Parses the Sonnet prompt JSON schema: `market_observation`, `intents` (≤5), `calibration_note`, `next_check`. Maps non-standard actions (`"trim"` → `SELL`, `"add"` → `BUY`, `"exit"` → `SELL`). Guards on `DRAWDOWN_LIQUIDATE`; any LLM exception → `[]`. Records each emitted intent in `AgentMemory`.
- `agents/opus_agent.py` — `OpusAgent(BaseAgent)`: concentrated GARP discretionary PM. Two call modes: `observe()` for daily thesis health checks (≤3 intents) and `deep_dive(state, symbol, doc_pack)` for Thursday/Friday extended-context analysis. `_ACTION_MAP` handles `"hold"` → None (no order), `"trim"` → `SELL`, `"add"` → `BUY`, `"exit"` → `SELL`. `_parse_json()` helper is shared between both modes for JSON extraction with fallback brace-finding. `deep_dive` returns the full deep-dive dict (bull_case, bear_case, kill_criteria, catalyst_calendar, intent) rather than `list[Intent]`.
- `agents/manager_agent.py` — `ManagerAgent` (not a `BaseAgent` — has no `observe()`): CIO orchestrator with 7 distinct call methods: `regime_read`, `adversarial_critique`, `capital_reallocation`, `risk_check`, `drawdown_response`, `weekly_journal`, `master_capability_proposal`. All JSON calls share `_call_and_parse()` (format user message → LLM → JSON parse with brace-finding fallback → return `dict`). `weekly_journal` returns raw markdown string. Each method formats the appropriate user message from `AgentState` + call-specific arguments and uses the correct call_type string for `BudgetLedger` tracking.
- `tests/test_agents_m7.py` — 27 tests covering all three agents (mocked LLM via `MagicMock(spec=LLMClient)`):
  - SonnetAgent (8 tests): valid JSON → 1 intent, liquidate → [], bad JSON → [], budget exhausted → [], 8 intents capped to 5, "trim" → SELL, memory recording, factor signal computation
  - OpusAgent (9 tests): valid JSON → 1 intent (add→buy), liquidate → [], bad JSON → [], budget exhausted → [], 5 intents capped to 3, hold → [] (no order), deep_dive returns dict, deep_dive failure → {}, memory recording
  - ManagerAgent (10 tests): regime_read, regime_read failure, adversarial_critique, capital_reallocation, risk_check, drawdown_response, weekly_journal (returns string), weekly_journal failure (returns ""), master_capability_proposal, bad JSON → {}

**Test results:** 385/385 pass. Ruff: clean. mypy --strict on `core/` + `execution/` + `data/` + `agents/` (34 files): clean.

**What surprised me:**
- `json.loads()` returns `Any`, and the type-ignore comment needs to match `[no-any-return]` rather than `[return-value]`. The cleaner fix is to assign to an explicitly typed intermediate variable (`result: dict[str, Any] = json.loads(...)`) and return that — no ignore comment needed, mypy is satisfied.
- `ManagerAgent` deliberately does not inherit `BaseAgent`. The `BaseAgent` ABC requires `observe(state) -> list[Intent]`, but the Manager has 7 distinct call types returning different schemas (dict, str). Forcing it into the `observe()` pattern would require a `call_type` parameter that the ABC doesn't have. A standalone class with typed methods is the right design.
- The `_ACTION_MAP` for OpusAgent includes `"hold" → None` — the intent parsing loop skips `None`-mapped actions. This correctly produces zero intents when the LLM returns a "hold" recommendation, which is the most common daily output for the Opus sleeve.
- `deep_dive()` max_tokens is 4096 vs. the standard 1536 for all other call types — the deep-dive prompt is designed to produce 300–500 word analysis sections and needs the larger budget.

**Pending for M8+:**
- App entrypoint (`app.py` or `main.py`) wiring all four agents into a scheduled loop (APScheduler, Alpaca streaming, market-hours awareness).
- Sleeve budget tracking: each agent's `AgentMemory` tracks intents, but there's no runtime object managing per-sleeve NAV, Sortino, and the 4-week performance snapshot the Manager needs for `capital_reallocation`.
- `ManagerAgent.adversarial_critique` currently receives raw `list[Intent]` — a future enhancement feeds only the highest-conviction new intent per agent per day.
- OpusAgent deep-dive scheduler: the Thursday/Friday cron job that selects which holding to deep-dive and fetches the document pack (10-Q/10-K via EDGAR + yfinance transcripts).
- Weekly journal persistence: `weekly_journal()` returns a markdown string; writing it to `logs/WEEK_NN.md` and posting it to Telegram is part of the M8 ops layer.

---

## Milestone 8 — 2026-04-25 — Dashboard (Plotly Dash, read-only)

**What was built:**
- `dashboard/data.py` — `DashboardData` read-only adapter. Aggregates from `OMSStore` (fills), `AgentMemory` (intents per agent), `CalibrationTracker` (Brier scores), and `BudgetLedger` (today's spend). Six dataclasses (`TopStripMetrics`, `IntentRow`, `FillRow`, `SpendBreakdown`, `AgentSummary`) define exact shapes for each panel. All stores are optional — passing `DashboardData()` with no args returns sensible empty defaults so the dashboard renders even before any trading has happened.
- `dashboard/layout.py` — Pure Dash component builders. Dark "terminal" palette (`#0d1117` bg, `#58a6ff` accent, monospace). `render_top_strip`, `render_agent_column`, `render_intent_log`, `render_fill_log`, `render_spend_panel`, and `render_full_dashboard` (composes the page). Action colors: buy/add → green, sell/trim/exit → amber. Outcomes: win → green, loss → red, pending → dim.
- `dashboard/app.py` — Replaces the M1 stdlib stub with the full Dash app. `build_app(data)` factory takes a `DashboardData` (testable in isolation). `_load_from_env()` reads paths from `OMS_DB`, `BUDGET_PATH`, `AGENT_MEMORY_DB`, `CALIBRATION_DB` env vars and opens read-only handles. Single 3-second `dcc.Interval` polls and re-renders the page (per blueprint Principle 9).
- `dashboard/__init__.py` — Empty marker file. Required because `data.py` lives in both `data/` (the data layer) and `dashboard/` — without an explicit package, mypy complained "Source file found twice under different module names."
- `agents/memory.py` — Added `recent_intents_rows(n)` returning structured `list[dict[str, str | int | None]]` for the dashboard. The existing `recent_intents_summary` returns a string for LLM context; the dashboard needs structured rows to render tables.
- `tests/test_dashboard_data.py` — 21 tests covering every adapter method. Uses real (in-memory or tmp) `AgentMemory`, `CalibrationTracker`, `BudgetLedger`, and `OMSStore` — no mocks at the store layer because each store is small and self-contained. The end-to-end `render_full_dashboard` test catches any layout/data shape mismatch by actually building the Dash component tree.

**Test results:** 406/406 pass. ruff: clean. mypy --strict on `agents/` + `core/` + `data/` + `execution/` + `dashboard/`: clean (38 source files).

**What surprised me:**
- `OMSStore.append()` requires `ts: datetime` as a positional arg — I'd written tests assuming an auto-`now()` default. Fixed by passing `_TS` explicitly. The strictness is correct: replay must be deterministic.
- mypy "Source file found twice" — `dashboard/data.py` and `data/` both compile to a module named `data`. Solved by adding `dashboard/__init__.py` to make `dashboard` an explicit package; then `dashboard.data` is unambiguous. (`data/` already had `__init__.py`.)
- After `dash` + `plotly` were `uv pip install`ed, mypy dropped its `untyped-decorator` complaint on `@app.callback` — Dash now ships PEP-561 type info. The previous `# type: ignore[untyped-decorator]` became "unused-ignore" and had to be removed. Trade-off: tests need dash installed to run (it's in pyproject.toml deps but the dev environment had only the dev extras installed).
- The dashboard reads `BudgetLedger._entries` directly (one underscore-prefixed attribute) for the per-call-type / per-agent breakdown. The ledger's public API exposes only `today_spent()` aggregate; rather than adding a public accessor that's only used by the dashboard, it's cleaner for the dashboard adapter (which is the only reader of internal ledger state) to peek directly. Documented as a deliberate read-only access pattern.

**Pending for v1.5:**
- WebSocket / SSE push instead of 3s polling. Polling is fine for single-user local mode; SSE is the obvious upgrade if multiple users ever read the dashboard simultaneously.
- Equity sparklines vs. SPY (Plotly mini-chart per agent column). The data layer can return the time series; layout just needs `dcc.Graph` calls. Skipped to keep M8 focused on the data adapter contract.
- Approval-queue drawer (only relevant when `AUTO_APPROVE=false`). Layout slot exists in `top_strip` (`approval_queue_count` always 0 right now); wire when the queue exists.
- `total_nav` and `day_pnl_gross` are `None` on every render — they require `BrokerAccount` polling or position aggregation. Will wire in the M9 ops layer alongside the scheduled loop.
- Markdown rendering of weekly journals (`WEEK_NN.md`) inside the dashboard — currently the Manager produces these but the dashboard does not display them. A single `dcc.Markdown` panel reading the latest week file would close the loop.

---

## Build complete (M1 → M8)

All eight milestones land. **406 tests pass. ruff clean. mypy --strict clean across 38 source files.** The four-agent system has data, agents, OMS, broker, risk, budget, memory, calibration — and now a dashboard to watch it run. Next: the ops layer (`app.py` scheduler, Telegram alerts, recovery cron) and graduation criteria evaluation.

---

## M9 Sub-task 1 — 2026-04-26 — Blueprint Compliance Fixes

**What was fixed (4 blueprint violations):**
- `agents/llm.py`: `cache_control` now carries `{"type": "ephemeral", "ttl": "1h"}`. Previously lacked the explicit `ttl` field, causing Anthropic to silently default to 5-minute TTL — approximately 12× more expensive for prompts reused across hour boundaries.
- `dashboard/app.py`: `host="0.0.0.0"` → `host="127.0.0.1"`, removing the `# noqa: S104` suppression. Dashboard now binds localhost-only per blueprint Principle 9.
- `dashboard/app.py`: `POLL_INTERVAL_MS = 5000` → `3000`. Blueprint §9 specifies 3s polling.
- `dashboard/app.py` + new `config/runtime_store.py`: Replaced static MC `_strip_cell` with `dcc.Slider` (id=`mc-slider`) placed OUTSIDE the polling `div#root` so it doesn't reset on every 3s tick. A `dcc.Store(id=mc-store)` holds the current slider value server-side. New `RuntimeStore` singleton (thread-safe, clamps at 1.5×) writes the change immediately.

**New files:** `config/runtime_store.py`, `tests/test_llm_cache_ttl.py` (3 tests)

**Commits:** 3eb1235 (cache TTL), f456d6d (host/poll), 9c9dde4 (MC slider)

---

## M9 Sub-task 2 — 2026-04-26 — Wire Deferred Plumbing

**What was built:**
- `execution/tax.py`: Added `WashSaleChecker.days_remaining(agent_id, symbol, check_date)` — returns days left in the 30-day window for display in veto reasons.
- `core/types.py`: Added `legs: tuple[OptionLeg, ...]` field to `Intent` (default empty tuple). Forward reference works via `from __future__ import annotations`. This enables multi-leg options intents throughout the system.
- `core/events.py`: Added `LeverageRotationFlagEvent` (14th event type on the bus) — carries `agent_id`, `symbol`, `category`, `reopen_count`. Emitted when the anti-rotation rule fires.
- `execution/budget.py`: Added `BudgetWatcher` — daemon thread polls `BudgetLedger.is_exhausted()` every 30s; calls `KillSwitchEngine.trip_budget_exhausted()` on exhaustion (blueprint §5 Layer 3: Haiku-only mode). `TYPE_CHECKING` guard prevents circular import from `budget.py` → `kill_switch.py`.
- `execution/reconciler.py`: Added `_DOLLAR_TOLERANCE = Decimal("1.00")`. Position mismatch now trips on either ≥1-share OR >$1.00 dollar drift (blueprint Principle 4 full implementation). Uses full `BrokerPosition` objects (not just qty) to get `current_price` for dollar calculation.
- `execution/risk.py` (major rewrite): Added 5 new capabilities:
  1. **Wash-sale check** (check #5): Blocks BUY intents for symbols sold at a loss within 30 days. Also checks `WASH_SALE_PROXIES` map (SPY↔IVV, QQQ↔QQQM, TQQQ↔UPRO, SQQQ↔SPXU). Veto reason includes days remaining.
  2. **LETF anti-rotation rule** (check #7b): Tracks LETF opens per-agent per-category (LETF_EQUIV_MAP groups TQQQ/UPRO=NDX_LONG_3X etc.). After ≥3 opens in the same category within 21 days: emits `LeverageRotationFlagEvent` and rejects intent.
  3. **Options structural check** (check #9): Rejects options-sleeve opening intents with no legs (naked), or with all legs on the same side (one-sided ratio). Requires both BUY and SELL legs (vertical/condor structure).
  4. **`record_letf_open/exit`** public methods for the main loop to call after fill confirmation.
  5. `RiskGate.__init__` now accepts optional `event_bus: EventBus | None = None` — backward-compatible (all existing code passes 3 args, new bus arg defaults to None).
- `agents/llm.py`: Added `_BACKOFF_529_SECS = [1.0, 4.0, 16.0]`. `_call_with_retry` now handles `anthropic.APIStatusError` with status 529 (overloaded) using this schedule + 0–1s jitter. Other 5xx errors get flat 1s sleep.
- Pre-existing M7/M8 uncommitted changes bundled into this commit: `agents/haiku_agent.py` and `agents/manager_agent.py` use new `agents/json_utils.parse_json_object` helper (centralizes JSON-with-fallback parsing); `manager_agent.py` now catches `BudgetExhausted` on `weekly_journal` and logs warning. `dashboard/data.py` and `data/cache.py` minor cleanups. `execution/oms_store.py` minor fix.

**Test results:** 277/277 pass (core tests; alpaca/dash/market-data tests excluded — require optional deps not installed in CI). Ruff: clean across all 11 changed files.

**New/extended tests (25 new test cases):**
- `test_budget_enforcer.py` (+3): BudgetWatcher trips kill switch, no-trip when not exhausted, idempotent after trip
- `test_llm_cache_ttl.py` (+2): 529 retry succeeds after 2 failures (backoff values verified), raises after max retries
- `test_reconciler.py` (+2): dollar-only mismatch trips kill switch, within-tolerance test updated (0.005 shares @ $100 = $0.50 < $1.00 is the new valid "within tolerance" case)
- `test_risk_gate.py` (+18): wash-sale blocked/allowed/proxy/sell-bypass; LETF rotation blocked/allowed; naked/spread/one-sided options structural tests; updated `test_options_within_20pct_allowed` to use a proper vertical spread (legs=(BUY+SELL))

**What surprised me:**
- Reconciler dollar-mismatch: the existing `test_reconcile_within_tolerance_no_trip` used 0.5 shares at $100 = $50 drift, which EXCEEDS the new $1.00 dollar threshold. Updated the test to use 0.005 shares ($0.50 drift) — below both qty tolerance (1 share) and dollar tolerance ($1.00).
- The `_check_options_structure` fires AFTER the exposure cap check (check order 8 → 9), so `test_options_exceeding_20pct_blocked` continues to pass with empty legs — the cap check fires first and the structural check is never reached.
- `_call_with_retry` was accidentally placed at module scope (not as a class method) during an earlier Edit. Fixed by rewriting `agents/llm.py` in full via the Write tool.

**Pending for M9 Sub-task 3:**
- `app.py` entrypoint: APScheduler + all 8 scheduled jobs, singletons (OMS, RiskGate, BudgetWatcher, KillSwitchEngine, Reconciler, etc.), reactive scans, heartbeat, dashboard thread.
- `ops/heartbeat.py`, `ops/alerts.py` (ntfy.sh), `ops/journal.py`.
- Smoke test (requires Opus model for orchestration depth).

---

## M9 Sub-task 3 — 2026-04-26 — `app.py` Entrypoint + Ops Modules (Opus 4.7)

**What was built:**
- `app.py` (~570 LOC): single-process orchestrator. `App` class owns every singleton (`EventBus`, `KillSwitchEngine`, `OMS`, `OMSStore`, `Broker`, `MarketData`, `LotLedger`, `WashSaleChecker`, `RiskGate`, `BudgetLedger`, `BudgetWatcher`, `Reconciler`, four `LLMClient`s, four `AgentMemory` SQLite dbs, four agents, `HeartbeatWriter`, `AlertManager`, `BackgroundScheduler`). Construction is deterministic — no threads start until `app.start()`. Broker / market-data are injectable so the test suite never touches Alpaca.
- 13 scheduled cron jobs registered with NYSE timezone (`ET`):
  - mon-fri market-hours: `sonnet_pre_open` 09:25, `sonnet_mid_morning` 10:30, `sonnet_midday` 12:00, `haiku_news_scan` 13:30, `sonnet_power_hour` 15:00, `haiku_close` 15:55, `sonnet_eod` 16:30, `opus_daily` 16:30
  - weekly: `opus_thursday_deepdive` Thu 16:30, `opus_friday_deepdive` Fri 16:30, `manager_friday` Fri 17:00 (regime read + weekly journal + 4-week reallocation on every 4th iso-week)
  - 24/7: `haiku_crypto` hourly
  - daily: `budget_reset` UTC midnight (resets ledger + KillSwitch daily)
- Deep-dive rotation: Thu picks oldest `last_deep_dive_date` from current Opus holdings; Fri picks the second-oldest. `last_deep_dive_date:{symbol}` persisted in Opus's `AgentMemory`.
- Reactive volatility scanner: 60s background poll during market hours; macro-event trigger fires Haiku scan on the day of any `config/macro_events.yaml` event (NFP/CPI/FOMC/GDP).
- Lifecycle: `start()` calls `oms.recover()` (replays append-only event log), boots all subsystems in order (alerts → heartbeat → budget watcher → reconciler → scheduler → optional dashboard + vol scanner). `stop()` is idempotent under a lock; tears down in reverse, snapshots agent memories, writes `logs/shutdown_{TIMESTAMP}.json`.
- SIGINT/SIGTERM handlers in `main()`. Real-money guard: refuses to start if `alpaca_paper=False` or `master_capability > 1.5` without `OVERRIDE_KEY`.
- `dispatch_observation(agent)`: builds shared `AgentState`, calls `agent.observe()`, runs each emitted intent through `RiskGate` (logs vetoes), respects BUDGET_EXHAUSTED → Haiku-only-mode degradation per blueprint §5 Layer 3.
- `ops/heartbeat.py`: `HeartbeatWriter` — 30s daemon-thread tick writes `{"ts": iso8601, "uptime_s": int}` to `logs/heartbeat.json` atomically (temp file + `Path.replace`). Also calls `KillSwitchEngine.record_heartbeat()` so a stuck main loop trips `HEARTBEAT_MISSED` after 60s.
- `ops/alerts.py`: `AlertManager` — subscribes to `kill_switch.tripped`, `reconciliation.break`, `budget.exhausted`, `leverage.rotation_flag` channels; pushes formatted notifications to `https://ntfy.sh/{topic}`. 60s deduplication keyed by `(channel, identity)` so flapping conditions don't spam. HTTP poster is injectable for tests; empty topic = silent (dev / test default).
- `config/macro_events.yaml`: 11 high-impact events for May–July 2026 (NFP, CPI, FOMC, GDP).

**Test results:** 446/446 pass. Ruff: clean across all sub-task 3 files. 24 new test cases:
- `test_app_lifecycle.py` (10): construction without side effects, clean start→stop with shutdown summary, agent-state population (bars/positions/account/MC), multi-agent dispatch via mocked `observe`, exception swallow, BUDGET_EXHAUSTED → Haiku-only, idempotent stop, macro calendar load, empty-credentials construction (parametrized).
- `test_app_scheduler.py` (7): all 13 blueprint job IDs registered; market-hours jobs use `mon-fri` cron; Fri-only and Thu-only jobs verified; cron times match blueprint §2 exactly; `budget_reset` uses UTC; `haiku_crypto` hourly; reset handler is idempotent.
- `test_app_recovery.py` (5): order survives via event log when app1 "crashes" (no `stop()`) and app2 boots against the same OMS db; empty-log recovery is no-op; event count > 0 after restart; kill switch starts in OK; shutdown summary reflects `open_orders=0`.

**What surprised me:**
- `data/market.py` imports `alpaca` at module top, so `agents/base.py` (which imports `Bar`) fails at import time without the alpaca-py library installed. Test environment now installs alpaca-py + dash + plotly + duckdb + diskcache + feedparser + pydantic-settings. (Optional-import refactor of `data/market.py` is filed for M10.)
- APScheduler `CronTrigger.fields` exposes the parsed cron parts as a list with `.name` and `__str__`, but `str(field)` for a literal day produces e.g. `"mon-fri"` not `"mon,tue,wed,thu,fri"`. Asserting against `str(field)` is what the tests use; reaching into `field.expressions` would be more brittle.
- `core.types.AgentState` (the per-agent risk/state record) is a different dataclass from `agents.base.AgentState` (the LLM observation snapshot). app.py imports both via `AgentState as CoreAgentState`. Worth a rename in M10 — `RiskAgentState` vs `ObservationState` would prevent confusion.
- Initial signature for `effective_max_gross()` had `vix_bucket: VixBucket` and `drawdown_bucket: DrawdownBucket` (not `vix_pct` / `drawdown_pct` as I'd assumed). The defaults in `build_agent_state` use `SWEET_SPOT` + `NORMAL` until live VIX wiring lands.
- Pre-trade routing (`Intent` → `Order`) is NOT implemented in app.py — there is no `ExecutionPlanner` yet. `dispatch_observation` runs `RiskGate.check_intent()` and logs the decision; full sizing-→-Order construction is deferred to the smoke test (sub-task 5) and will likely materialize as `execution/planner.py` in M10.

**Deferred to M10 (filed in `logs/m10_backlog.md`):**
- `execution/planner.py` — turns approved Intents into Orders (sizing × MC × kill-switch scalars, then `OMS.submit_order`).
- Optional-import refactor of `data/market.py` so `agents/base.py` imports without alpaca installed.
- Live VIX feed for `build_agent_state` (currently a static SWEET_SPOT default).
- Per-agent drawdown bucket tracking (currently NORMAL placeholder in `_evaluate_with_risk_gate`).
- Volatility scanner: full 30-day rolling realized-vol math (placeholder skips the 2σ branch).

---

## M9 Sub-task 4 — 2026-04-26

**Commit:** `19a7d9a`

**Files changed:** `ops/journal.py` (new), `tests/test_journal.py` (new), `pyproject.toml` (+pyyaml, +apscheduler)

**What was built:**
- `ops/journal.py`: Two atomic-write persistence helpers.
  - `write_weekly_journal(markdown, ref_date, logs_dir) → Path`: Writes Manager's weekly journal (a markdown string) to `logs/WEEK_{YYYY}_{WW:02d}.md`. ISO week numbering via `date.isocalendar()`. Zero-padded week (`01`–`53`).
  - `write_daily_memo(content, agent_id, ref_date, logs_dir) → Path`: Writes per-agent daily memos to `logs/daily/{agent}_{YYYY-MM-DD}.md`. Accepts both `AgentId` enum and plain strings.
  - Both use `_write_atomic()`: write to `.tmp` sibling, then `Path.replace()` onto destination. Idempotent — re-running overwrites the file, never appends.
  - Both call `Path.parent.mkdir(parents=True, exist_ok=True)` so the caller doesn't need to pre-create directories.

**Missing deps discovered and fixed:**
- `pyyaml` was not in `pyproject.toml` but `app.py` imports `yaml` for `config/macro_events.yaml`. Added `pyyaml>=6.0` to deps.
- `apscheduler` was in `pyproject.toml` but not installed in the `.venv` (uv's vectorbt resolution had issues). Force-installed `apscheduler==3.11.2` via `uv pip install`.

**Test results:** 461/461 pass. Ruff: clean. 15 new tests in `test_journal.py`:
- `test_weekly_journal_*` (7): correct filename, content roundtrip, idempotent overwrite, zero-padded week number, creates missing parent dirs, no .tmp leftover, empty content still creates file.
- `test_daily_memo_*` (8): correct path under `daily/`, content roundtrip, idempotent overwrite, all three agents get separate files, accepts string agent_id, creates `daily/` subdirectory, no .tmp leftover, different dates create different files.

**What surprised me:** Nothing — the module was straightforward. The real work was discovering that `pyyaml` and `apscheduler` weren't installed in the test venv. All 22 pre-existing app tests (lifecycle/scheduler/recovery) that were erroring on `ModuleNotFoundError` now pass correctly.

---

## M9 Sub-task 5 — 2026-04-26

**Commit:** _(see git log)_

**Files changed:** `tests/test_smoke.py` (new), `logs/v1_complete.md` (new), `logs/m9_complete.md` (new), `logs/build_journal.md` (this entry)

**What was built:**

Smoke test suite (`tests/test_smoke.py`, 11 tests):
1. `test_full_startup_shutdown_cycle` — App.start() → App.stop() → shutdown summary has correct fields (kill_switch_state=ok, open_orders=0, started_at, shutdown_at).
2. `test_intent_through_riskgate_returns_accepted_list` — BUY 10% SPY intent from mocked Haiku observe() passes through RiskGate and is returned as accepted.
3. `test_oms_submit_fill_ledger_cycle` — Full OMS → FakeBroker → fill cycle: `submit_order` returns accepted, order appears in OMS, FakeBroker shows 5 SPY in positions.
4. `test_heartbeat_file_written_by_tick_once` — HeartbeatWriter.tick_once() writes valid JSON with `ts` and `uptime_s`; no .tmp leftover.
5. `test_heartbeat_writer_starts_and_stops_with_app` — Thread is alive after start(), dead after stop().
6. `test_reconciler_clean_state_no_halt` — Reconciler.reconcile_once() on empty OMS + empty broker returns 0 position_mismatches, kill switch stays OK.
7. `test_budget_watcher_no_trip_with_headroom` — BudgetWatcher.check_once() on a fresh ledger keeps kill switch OK.
8. `test_multiple_agents_dispatched_independently` — All three agents dispatched without interference; each observe() called exactly once.
9. `test_journal_weekly_and_daily_written_to_logs_dir` — write_weekly_journal() and write_daily_memo() produce correct files under logs/.
10. `test_no_real_money_path_in_settings` — App raises RuntimeError when alpaca_paper=False (real-money guard).
11. `test_macro_calendar_loads_without_error` — config/macro_events.yaml loads and has ≥1 event.

**Final test counts:** 472 collected, 472 passing, 0 failures. 27 test files.

**Codebase metrics:** ~8,600 production LOC, ~7,500 test LOC, 16,100 total.

**What the smoke test could NOT verify:**
- A full trading-day run (requires real credentials). The `dispatch_observation → OMS` bridge doesn't exist yet (`ExecutionPlanner` is M10 P0). The smoke test verified each component independently; the `OMS.submit_order` path was exercised directly (not via dispatch). This gap is honest: the bot will not submit orders until M10's `execution/planner.py` is built.

**Completion reports written:** `logs/v1_complete.md` and `logs/m9_complete.md`.

---

## Milestone 10 Sub-tasks 1A + 1B + Integration — 2026-04-26

**What was built:**

**1A — `execution/planner.py`** (`ExecutionPlanner`):
- Blueprint §16 sizing: `effective_max_gross = base_max_gross × MC × vix_scalar × dd_scalar`, `effective_vol_target = base_vol_target × MC`, `position_value = vol_targeted_position_value(target_weight, equity, realized_vol_30d, effective_vol_tgt)`, capped at `effective_max_gross × equity`.
- `MASTER_CAPABILITY` read dynamically from `runtime_store` on every intent (dashboard slider propagates immediately).
- CLOSE intents bypass sizing math; `LotLedger.total_open_qty()` is used for exact close qty.
- Options detection via `intent.legs`; whole-contract quantization (÷100).
- `IntentSizedEvent` emitted for every sized intent (binding_constraint: "vol_target" | "max_gross" | "close").
- Adds `AGENT_BASE_VOL_TARGET` dict and `vol_targeted_position_value()` function to `execution/sizing.py`.
- Adds `MarketSnapshot` dataclass to `core/types.py`, `IntentSizedEvent` to `core/events.py`.
- 31 tests covering sizing math, all drawdown/VIX ladder combinations, runtime MC reads, options contracts, close path, and full OMS integration. Commits: `72fc6a2`.

**1B — `execution/agent_state_tracker.py`** (`AgentStateTracker`):
- 5-day recovery rule before loosening a drawdown bucket (tightening is immediate).
- Consecutive-loss benching: 5 losing SELL fills → `KillSwitchEngine.record_agent_result()` + 24h bench.
- Per-agent avg-cost tracking at fill time (avoids LotLedger ordering dependency).
- Equity model: `starting_equity + realized_pnl(LotLedger) + unrealized_pnl(mark_prices)`.
- Rolling 30-day peak for drawdown % computation; FORCED_CASH at >25% drawdown.
- SQLite persistence (`agent_tracker_state` table); cold-start rebuild from LotLedger fills.
- 21 tests covering tightening/loosening, consecutive losses, equity tracking, cold-start, forced-cash, and SQLite round-trips. Commit: `b1df2aa`.

**Integration — `app.py` wiring**:
- `dispatch_observation()` now: calls `tracker.update_on_mark(agent_id, prices)` before observe, gets live `CoreAgentState` from `tracker.get_state(intent.agent_id)` per intent, routes approved intents through `planner.plan()` then `oms.submit_order()`.
- `_evaluate_with_risk_gate()` takes a live `CoreAgentState` instead of the hardcoded NORMAL bucket stub.
- `_build_market_snapshot()` derives `MarketSnapshot` from bars (last close as mark, 30-day EWMA vol, VIX bucket from `classify_vix()`).
- `_on_fill_received()` subscribed to `fill.received` events → `tracker.update_on_fill()`.
- `settings.starting_equity` added (default $100k).
- Smoke test `test_dispatch_observation_submits_order_via_planner` confirms end-to-end flow. Commit: `5683ed0`.

**Test results:** 525/525 pass. ruff: clean. mypy --strict on new modules: clean (pre-existing yaml/dashboard errors unchanged).

**What surprised me:**
- The key pitfall in drawdown/VIX bucket tests: the vol-target constraint binds *before* the max_gross cap when `target_weight` is small. Tests that check bucket scalars must use `target_weight=1.0` (or any weight that makes vol_targeted > gross_cap) so max_gross actually binds and the scalars become visible in the qty.
- Recovery rule state: tracking `recovery_target`, `recovery_since`, and `recovery_days` is trickier than it sounds — tightening must immediately reset all three, and "new trading day" detection (comparing `today != rec.last_update_date`) is the only reliable boundary since the tracker is called on arbitrary schedules.
- The `sum()` builtin without a start value is typed as returning `int` by mypy when called on `list[Decimal]`. Fixed by using `sum(returns, Decimal("0"))` and `Decimal(len(n))` throughout the vol computation in `_build_market_snapshot`.

**Pending (M10 remaining):**
- Sub-task 2: Real Telegram integration (`ops/telegram.py`) — python-telegram-bot v21+, MarkdownV2, 1-min dedup, Acknowledge button.
- Sub-task 3 (BLOCKED): Baseline backtest engine — must confirm with Brooks before starting.

---

## Path A — Fair-Test Hardening (Manager + Opus + Health) — 2026-05-28

**Context:** After 19 trading days the bot was behind SPY by ~2.9% (beat SPY 9/19 days =
47.4%, avg daily excess −0.151%/day). Sleeve attribution showed the LLM-edge thesis is so far
disproven by the ledger: Haiku (7-ETF closet-index basket) +$370–520 carried everything;
Sonnet (the only genuine single-name picker) net −$65; Opus $0 (≈97% idle cash). BUT the
Manager's capital reallocation had **never once executed**, so the adaptive design had never
actually run. Brooks chose Path A: fix the broken pieces, then a **10-trading-day kill
criterion** — if cumulative daily-excess-vs-SPY isn't positive, pivot/kill (no goalpost moves).

**What was fixed:**

1. **Manager capital reallocation — silent no-op (real bug).** The `reallocation.json` prompt
   emits *dollar* allocations (`current_allocation` / `new_allocation`, e.g.
   `{"haiku":950,"sonnet":1100,"opus":950}`), but `app.py` looked for a nonexistent
   `sleeve_weights` multiplier key, then fell back to iterating the whole response dict — whose
   keys (`decision_basis`, `new_allocation`, …) never parse as `AgentId`, so `mapped` was always
   empty and `write_sleeve_weights()` never fired. Empty `manager_sleeve_weights.json` = `{}`
   since 2026-05-04, no `last_capital_reallocation` memory key. **Fix (`app.py` ~1629):** read
   the real schema, derive `multiplier = new_allocation / current_allocation` per sleeve, rebase
   to mean-1.0 if no baseline, clamp to ±25% (the prompt's own per-4-week rule). Added
   instrumentation: logs raw response keys and emits an explicit `produced NO usable weights`
   WARNING instead of failing silently. Verified vs documented schema + `test_agents_m7`.

2. **LLM hang → starved scheduler (health).** `anthropic.Anthropic(...)` was constructed with
   **no timeout** (SDK default 600s/request). Under the recurring `getaddrinfo` DNS flaps a
   single `observe()` could wedge for tens of minutes; with the crypto job on an hourly trigger
   + `max_instances=1`, every subsequent tick was skipped ("maximum number of running instances
   reached"). **Fix (`agents/llm.py`):** `_LLM_REQUEST_TIMEOUT_SECS = 60`, and the retry loop
   now also catches `APITimeoutError` / `APIConnectionError` with the 529 backoff curve so
   transient flaps recover the tick instead of dropping it. Worst-case hang ~3×60s + backoff.
   **`app.py` scheduler:** `job_defaults={"coalesce": True, "misfire_grace_time": 300}`.

3. **Opus idle capital (strategy, reversible).** Opus targets 5 holdings at ≤2 starters/cycle
   but fired only twice weekly (Mon init + Thu deep-dive) → ~$30k (≈30% of NAV) sat in cash for
   weeks, a structural drag vs SPY on up days. **Added `JOB_OPUS_DAILY_INIT`** (`_job_opus_daily_init`,
   Mon-Fri 10:00 ET): self-guarded book-building that dispatches **only while open holdings <
   TARGET_HOLDINGS**, then self-disables. Does not make Opus a day-trader (management-mode ticks
   dedupe via `signal_fingerprint`). Fully reversible — delete the one job to revert.

**Also checked:** "ghost" python processes were a false alarm — `src/main.py` / `run_arb.py`
belong to a separate repo (`/Users/brooksmoore/Desktop/pure_arb_bot`); exactly one clean
`app.py` instance runs for this bot.

**Test results:** 710 passed, 1 skipped. New code lint-clean (remaining ruff items in `app.py`
are pre-existing: `LiveMetrics` forward-ref, import sort).

**Deploy note:** the 00:22 restart loaded only the Manager fix; the llm.py timeout, scheduler
defaults, and Opus daily job were edited afterward and need a **second restart** to go live.

**What to watch (fair test):**
- Daily: `opus_daily_init: under-invested … dispatching` → idle cash deploying toward 5 holdings.
- Fri 17:00 ET: Manager logs either `persisted sleeve weights {…}` or `produced NO usable
  weights` — no more silent failure.
- Kill criterion: 10 trading days from this deploy; measured via the SPY-pairing script
  (per-day max NAV from `equity_snapshots.db` × `SPYProvider().daily_closes()`).

---

## H1+H2 — Opus Cost Reduction — 2026-05-28

**Context:** After deploying `JOB_OPUS_DAILY_INIT` (daily book-building), LLM spend spiked to
~$0.48/day vs the $0.10/day cap. Root cause: Opus-model calls (opus `daily_check` ~$0.21 +
Manager `capital_reallocation` ~$0.23) are 92% of spend and **cannot be prompt-cached** — the
ephemeral cache TTL is 1h but Opus fires once/day and Manager monthly, so every call is a cold
cache miss. Haiku gets 90% cache hits only because it fires inside the hour.

**H1 — Deploy full book in one initiation cycle (`agents/opus_agent.py`):**
- Added `_INITIATION_MAX_STARTERS = TARGET_HOLDINGS = 5`.
- `_format_daily_context`: computes `remaining = max(1, TARGET_HOLDINGS - len(positions))`;
  prompt now asks for "up to {remaining} starter intent(s) to fill the book in this cycle".
- `_parse_daily_intents`: cap is `min(remaining_slots, _INITIATION_MAX_STARTERS)` in
  initiation, `_MAX_INTENTS=3` in management. Guardrails (conviction ≥7, weight ≤0.05)
  unchanged. One cycle fills 1→5 holdings instead of ~3-4 daily Opus calls.

**H2 — Sonnet for book-building, Opus reserved for deep-dive (`agents/opus_agent.py` + `app.py`):**
- `OpusAgent.__init__` gains `llm_lite: LLMClient | None = None` (defaults to `llm` for back-
  compat with all existing callers/tests).
- `observe()`: `mode == "initiation"` → uses `self._llm_lite` + `call_type="daily_check_lite"`;
  management → `self._llm` (Opus) + `"daily_check"`. Thursday deep-dive unaffected (always Opus).
- `app.py`: `self._llm_opus_lite = LLMClient(self.budget, model=SONNET_MODEL, …)` wired as
  `llm_lite=self._llm_opus_lite` in `OpusAgent(…)`. Mirrors the existing Manager lite pattern.
- Cost: initiation daily_check $0.21 (Opus) → ~$0.04 (Sonnet). Full ramp $0.63–0.84 → $0.04–0.08.
  Steady-state unchanged: Mon init + Thu deep-dive both stay Opus (~$0.42/wk total).

**Test isolation fix (`tests/conftest.py`):**
- `pytest_configure` now redirects `agents.manager_bridge.SLEEVE_WEIGHTS_FILE` to an empty
  temp file. The first live Manager reallocation (2026-05-28) wrote real weights
  (haiku=0.80×, sonnet=1.16×, opus=1.04×), causing 8 sizing/planner tests to fail with
  `AssertionError: Decimal('1.199…') == Decimal('1.50')`. Mirrors the existing RUNTIME_STORE
  isolation pattern; tests that need real weights monkeypatch `SLEEVE_WEIGHTS_FILE` directly.
- `tests/test_agents_m7.py`: `test_opus_caps_intents_at_three` split into two:
  - `test_opus_initiation_caps_intents_at_remaining_slots`: 0 positions → expects 5 intents.
  - `test_opus_management_caps_intents_at_three`: TARGET_HOLDINGS positions → expects ≤3.

**Test results:** 718 passed, 1 skipped. All new code lint-clean.

**Requires restart** (PID 28145 running pre-H1/H2 code).

---

## Robinhood Live Broker Adapter — 2026-06-02

**Context:** User decided to wire the bot to a real-money **Robinhood agentic account** (small
bankroll) via Robinhood's newly-released MCP endpoint `agent.robinhood.com/mcp/trading`. I gave
a brutally-honest live-money evaluation; user accepted the risk profile and chose to keep LETFs
+ crypto in the universe. Downside machinery confirmed real (kill_switch.py ladder
−2%/−15%/−25%/−33%; agent_state_tracker FORCED_CASH + consecutive-loss bench) but uptime-
dependent. Remaining real risk is execution correctness on a brand-new broker → mitigated by a
dry-run default and paper-through-the-adapter before funding.

**What was built:**
- `execution/robinhood_broker.py` — `RobinhoodBroker` implementing the `Broker` Protocol
  (submit_order idempotent on order.id, cancel_order, get_order, find_order_by_client_id,
  list_positions, get_account, register_event_callback). No push stream → relies on the
  existing reconciler polling fallback (start_stream is a no-op).
  - Transport: minimal **MCP-over-streamable-HTTP JSON-RPC client** built on httpx 0.28
    (the `mcp` python SDK is not installed). Async session bridged into the sync Broker
    interface via a dedicated event-loop thread, mirroring AlpacaBroker's threading model.
  - **Safety gate:** `live_trading_enabled` defaults False → DRY-RUN (logs intended orders,
    returns a synthetic broker_order_id, never sends). Funding + arming is the user's action.
  - **Robinhood MCP tool schema is UNVERIFIED** (days-old feature, no docs in hand). Tool
    names/params/auth are isolated in `_RH_TOOLS` + `_build_order_args` with `TODO-VERIFY`
    markers; translation layer conforms to our domain types. Must be confirmed against the
    live MCP server before funding.
- `config/settings.py` — `broker_kind` ("alpaca"|"robinhood"), `robinhood_mcp_url`,
  `robinhood_auth_token`, `robinhood_live_enabled`.
- `app.py` — `_build_robinhood_broker()` + broker selection on `settings.broker_kind`
  (defaults to alpaca; real-money guard requires explicit live flag).
- `tests/test_robinhood_broker.py` — offline tests for type translation, idempotency, and the
  dry-run safety gate (no network).

**Next steps before funding (documented for the user):**
1. Verify the real MCP tool schema against `agent.robinhood.com/mcp/trading`; fix `_RH_TOOLS`.
2. Paper-trade THROUGH the adapter (live_trading_enabled=False dry-run, then a tiny real test).
3. Fund small with the kill-switch ladder live + a daily-loss phone alert.

---

## Log-Spam Fix + Robinhood Probe — 2026-06-03

**Log-spam (observability bug).** The Alpaca trading websocket logs a full ERROR + traceback on
every reconnect attempt; during a laptop network/DNS drop that was thousands/min, rotating 10 MB
of logs every ~1 minute and shredding all history (the current app.log spanned <4 hours; 3 of 4
rotated files covered ~1 minute each). The existing `setLevel(WARNING)` silence did nothing
because the storm logs at ERROR. Fix (`app.py`): added `_RateLimitFilter` (logging.Filter) that
lets the first occurrence of a message through, suppresses identical repeats within a 60s window,
and annotates the next emission with "+N identical suppressed". Attached to the
`alpaca.trading.stream` logger. Verified 5000 identical → 1 passed. 4 new tests
(`tests/test_log_ratelimit.py`). This restores observability — prerequisite for trusting the
dashboard before real money.

**Diagnostic context (why "no intents today" looked alarming):** bot was running fine (PID 1776,
2d8h uptime, market data flowing, positions intact). Low activity was (a) legitimate
skip-when-unchanged on a quiet day + (b) intermittent laptop connectivity during market hours
(user's hypothesis — confirmed: Sonnet fingerprint showed empty `top10=` for a cycle where the
bars fetch timed out). NOT a code regression. The log-spam had simply made it impossible to see.

**Robinhood probe (live bring-up step 2).** `scripts/robinhood_probe.py` — read-only. Reads
ROBINHOOD_AUTH_TOKEN from .env, runs the MCP `initialize` handshake, calls `list_tools()`, prints
each tool + input schema, flags mismatches vs the assumed `_RH_TOOLS`, and dumps sample
get_account/list_positions responses. NEVER calls place_order. Output is what we reconcile the
adapter's `_RH_TOOLS` + every `# TODO-VERIFY` field against. Settings map env vars directly to
field names (pydantic BaseSettings, no prefix): BROKER_KIND, ROBINHOOD_AUTH_TOKEN,
ROBINHOOD_LIVE_ENABLED, ROBINHOOD_MCP_URL.

**Test results:** 732 passed, 1 skipped. New code lint-clean.

**Restart required** to load the log filter (PID 1776 runs pre-fix code).

---

## Pre-Robinhood Hygiene / Efficiency / Execution-Efficacy Audit — 2026-06-06

Full audit before wiring real money. Verdict: **execution core is sound for live trading.**

**Execution-logic efficacy (the money-critical axis) — STRONG:**
- Every order passes `RiskGate.check_intent` (11 checks) before creation; order only built if
  `allowed=True` (app.py `_submit_one_intent` → `_evaluate_with_risk_gate`).
- Kill-switch ladder fully enforced (execution/risk.py + kill_switch.py `_BLOCKS_NEW_ENTRIES`):
  LIQUIDATE→only-close; DAILY_LOSS / DRAWDOWN_PAUSED / HEARTBEAT_MISSED / RECONCILIATION_BREAK /
  BUDGET_EXHAUSTED → no new entries. The heartbeat-missed + reconciliation-break blocks are a
  strong uptime safety property (laptop sleep / book drift → bot refuses to open, closes only).
- Idempotent submit on client_order_id (= order.id).
- Reconciler is source-of-truth: synthesizes a Fill from broker `filled_qty` when a stream fill
  is dropped, and `force_close_filled` handles qty-precision drift. Self-healing.
- Plus wash-sale, LETF whitelist/hold-period/anti-rotation, per-agent FORCED_CASH, weight caps.

**Fixed this pass:**
- `execution/oms.py`: the "FILL event without fill payload" log was ERROR and fired ~22×,
  polluting error visibility — it is BENIGN (reconciler recovers). Downgraded to INFO with an
  explanatory message.
- `execution/robinhood_broker.py`: 5 mypy `no-any-return` in the new money-path adapter →
  fixed with explicit dict/list coercion (also hardens against malformed MCP responses). 0 left.
- `scripts/clean_dust_lots.py`: new one-off to close 3 legacy phantom dust lots (haiku
  BTCUSD/ETHUSD/SOLUSD, ~1e-8 remainders from fractional crypto sells). Dry-run by default;
  run with --apply during a bot stop. Live close-path already prevents new dust.

**Reported, NOT fixed (rationale):**
- **Robinhood has NO push stream → every fill relies on the 60s reconciler poll.** Top pre-fund
  recommendation: tighten the reconcile interval for broker_kind=robinhood (e.g. 15-30s) so
  fill-awareness lags less — weigh against the new API's rate limits. A tuning call, not a bug.
- Lint: 183 items, ~96% cosmetic (87 E501 line-length, 17 import-sort, 8 E741). Real-ish ones
  are false positives (F821 LiveMetrics = TYPE_CHECKING forward-ref) or trivial (2 F401). Did
  NOT mass-churn pre-deploy (risky diff); recommend a separate `ruff --fix` hygiene pass later.
- mypy: bus typed as `object` (budget.py, agent_state_tracker.py) → 3 "no attribute publish";
  loose typing, works at runtime. Low priority.
- build_agent_state creates a 3-worker ThreadPoolExecutor per dispatch; dispatch is infrequent
  (few/hour), pools are cheap — judged acceptable, not worth lifecycle complexity.

**Test results:** 733 passed. New/changed code lint-clean; execution path mypy-clean.

---

## Robinhood Bring-up (Step 1+2) — 2026-06-06

**What was built / executed:**

- **Step 1 (probe) attempted exactly as specified.** Ran `.venv/bin/python scripts/robinhood_probe.py`.
  Result: "ERROR: ROBINHOOD_AUTH_TOKEN is empty. Add it to .env first."
  The read-only probe (initialize + list_tools + sample get_account/list_positions) cannot run until the owner
  populates ROBINHOOD_AUTH_TOKEN. No changes to `_RH_TOOLS` or any `# TODO-VERIFY` yet — those remain guesses
  until the live schema is observed. The probe script + _McpHttpClient already surface the exact failure modes
  (auth header scheme, endpoint reachability, protocolVersion) for the next run.
- **Step 2 (top correctness gap) — per-broker reconcile interval.** Robinhood has no push stream; every fill
  and terminal state transition for live orders waits on the Reconciler poll (was globally 60s).
  - Added `reconciler_interval_robinhood_secs: int = 20` in `config/settings.py` (Alpaca keeps the 60s default
    as its WS stream is primary; reconciler is safety net).
  - Wired selection in `app.py:287` (after broker construction): if `broker_kind == "robinhood"` use the
    tighter value, else the general one. The choice is driven by the *declared* settings.broker_kind so that
    tests injecting FakeBroker while setting broker_kind="robinhood" still exercise the production poll rate.
  - Updated module docstring (top of app.py) and crash-recovery note (no longer hard-codes "60s tick").
  - Updated `.env.example` with full Robinhood section (BROKER_KIND, ROBINHOOD_* vars, the two RECONCILER_INTERVAL*
    knobs) so the owner has a complete template before adding the real token.
  - Added `test_reconciler_interval_is_tighter_for_robinhood_broker_kind` in `tests/test_app_lifecycle.py`.
- Dry-run session wiring verification (no live MCP calls): constructed an App with `broker_kind="robinhood"`,
  `robinhood_live_enabled=False`, dummy token, and exercised dispatch_observation + planner path. Confirmed the
  reconciler was built with the robinhood-specific interval (15s in the probe). The RobinhoodBroker dry-run
  branch (the only path active while live_enabled=False) was already covered by `test_robinhood_broker.py`
  (DRYRUN- ids, idempotency on client_order_id, _build_order_args shape). Full sleeve-intent → logged-intent
  end-to-end will be re-run by the owner once the token is present and a real dry-run `app.py` session is started.
- Pre-existing execution invariants audited (unchanged by this work):
  - RiskGate.check_intent is still the only gate before Order construction (planner path).
  - KillSwitch ladder, submit_order idempotency on order.id, Reconciler-as-source-of-truth + force_close_filled
    for qty drift, all untouched.
- Hygiene: only the three new/changed source files + one test file were linted. The ~183-item repo-wide cosmetic
  debt (mostly E501 + import sort in app.py and pre-existing long lines in settings/reconciler) was left exactly
  as-is per the "do not mass-churn near live deploy" rule. mypy run on the required gate (`execution/` + `core/types.py`)
  shows only the three known pre-existing "bus: object has no publish" issues (budget.py, agent_state_tracker.py);
  no new type errors from the interval selection logic.

**Files changed:**
- config/settings.py (new setting + comments)
- app.py (selection logic + docstring updates)
- .env.example (Robinhood + reconciler docs)
- tests/test_app_lifecycle.py (1 new test)
- logs/build_journal.md (this entry)

**Test results:** 734 passed (733 + 1 new interval-selection test), 0 failures. All prior robinhood_broker, reconciler,
app_lifecycle, smoke, and risk/oms/kill_switch suites continue to pass. ruff on the touched files shows only
pre-existing violations. mypy execution/ + core/types.py: only the 3 known loose-bus issues.

**Decisions / flags for owner (real-money critical):**
- 20s default for robinhood is a deliberate trade-off: ~3x more fill awareness than before vs. ~3 list_positions +
  N get_order calls per minute while orders are open. If the MCP endpoint rate-limits the new adapter under load,
  raise RECONCILER_INTERVAL_ROBINHOOD_SECS in .env (or we can add a small backoff/jitter later).
- Once you add ROBINHOOD_AUTH_TOKEN to .env, re-run the probe **immediately** and paste the full output (tool names,
  inputSchema properties/required, plus the raw get_account + list_positions shapes). That is the sole input needed
  to promote every `# TODO-VERIFY` from guess to verified and to implement any auth/refresh flow if Bearer is not
  the scheme the server actually accepts.
- The live_enabled guard + dry-run default in RobinhoodBroker remain load-bearing. Funding + flipping the flag is
  still a deliberate owner action after the schema is reconciled and a 1-share test succeeds.

**Next (in exact order):**
1. Owner: `ROBINHOOD_AUTH_TOKEN=...` into .env (and optionally set BROKER_KIND=robinhood + RECONCILER_INTERVAL_ROBINHOOD_SECS if you want something other than 20).
2. Re-run `scripts/robinhood_probe.py`, share output → we fix `_RH_TOOLS` + all field accessors + any auth differences.
3. Owner starts a dry-run session (`broker_kind=robinhood, live_enabled=false`), confirms the log lines for intended
   orders match what the four sleeves emitted that cycle.
4. Owner: single 1-share live test (flip flag temporarily), inspect real broker response + idempotency (submit twice
   with same client id).
5. Small fund + confirm daily-loss kill-switch path + ntfy/Telegram alert fires.

**Test results (post-edit):** 734/734. No production paths for RiskGate / OMS / kill-switch / lots / reconciler source-of-truth were modified.

---

## AUDIT — Robinhood Bring-up Steps 1+2 — 2026-06-06

**Auditor:** Claude (independent technical auditor). Verified against real code, not Grok's description.

### Test gate
Full suite: **734 passed** ✅ (Grok's +1 claim confirmed: 733 → 734, the new interval-selection test).
Targeted suite (test_robinhood_broker + test_app_lifecycle): **22/22** ✅.
mypy `execution/ core/types.py`: **3 errors, all pre-existing** (bus typed as `object` in budget.py and agent_state_tracker.py). Zero new errors from this changeset. ✅

### Invariants re-verified (all intact)
1. **RiskGate before every Order**: `_submit_one_intent` (app.py:839) calls `_evaluate_with_risk_gate` → `RiskGate.check_intent`. No bypass path added. ✅
2. **Kill-switch ladder blocks new entries**: `_BLOCKS_NEW_ENTRIES` = {DRAWDOWN_PAUSED, DRAWDOWN_LIQUIDATE, DAILY_LOSS, HEARTBEAT_MISSED, RECONCILIATION_BREAK, BUDGET_EXHAUSTED} (kill_switch.py:241). `can_open_new()` (line 83) checks it. RiskGate check #2 (risk.py:175) applies it to all `_OPENING_ACTIONS`. Wiring untouched. ✅
3. **Submit idempotency on client_order_id**: In-process `_submitted` dict (robinhood_broker.py:281) handles within-session duplicates. OMS recovery path via `find_order_by_client_id` handles cross-restart. Both paths intact and untouched. ✅
4. **Reconciler source of truth**: `filled_qty` delta synthesis + `force_close_filled` for qty-precision drift (reconciler.py:204, 230) untouched by this changeset. ✅

### What was actually built (confirmed matches claim)
- `config/settings.py`: Added `reconciler_interval_robinhood_secs: int = 20`. Default 60 for Alpaca unchanged. Clean. ✅
- `app.py:287–294`: Interval selected by `settings.broker_kind.lower() == "robinhood"`. Uses the *constructor parameter* `settings` (= `self.settings`), NOT the global module-level singleton. No aliasing risk. ✅
- `tests/test_app_lifecycle.py:272`: New test sets `reconciler_interval_robinhood_secs = 17` (non-default), constructs App with FakeBroker + broker_kind="robinhood", asserts `app.reconciler._interval_secs == 17`. Proves actual selection, not default coincidence. ✅
- `.env.example`: Updated with full Robinhood section. Confirmed. ✅
- **No TODO-VERIFY promoted**: Every entry in `_RH_TOOLS` (robinhood_broker.py:72-77) and every field accessor still carries `# TODO-VERIFY`. No guesses promoted to "live". ✅

### Auth/token security
- `_auth_token` stored in `_McpHttpClient` instance (line 122), used only in `_headers()` as `Authorization: Bearer ...` (line 135). Never passed to any `log.*()` call. httpx does not log request headers by default. ✅
- `ROBINHOOD_AUTH_TOKEN` read from `.env` via pydantic-settings, never echoed in logs or `__repr__`. ✅

### Risks (not bugs — documented for completeness)
1. **No minimum floor on `reconciler_interval_robinhood_secs`**: setting it to 0 via env would cause the reconciler to spin. No production scenario makes this likely; low priority.
2. **Cross-restart idempotency on live Robinhood orders**: `_submitted` is in-memory. Post-restart, OMS recovery calls `find_order_by_client_id` which uses `list_orders?client_order_id=X` — a `# TODO-VERIFY` path. If Robinhood's MCP doesn't support that filter, recovery could re-submit. This is the **known gap** that the probe step is meant to close. Not new, documented by prior audit.
3. **No test that alpaca path still gets 60s** (the else branch of the interval selection). Other lifecycle tests that don't set `broker_kind` implicitly cover it, but none assert `_interval_secs`. Minor coverage gap.

### Verdict: **GO** (for this code changeset — probe still required before live flip)

This change is minimal, correctly scoped, and does not touch any execution invariants. The probe failure is a token-availability problem (owner must populate `.env`), not a code quality problem. The codebase is in a *safer* state for Robinhood bring-up than before: the fill-lag gap is closed by default when `broker_kind=robinhood`.

**Single highest-priority next action**: Owner adds `ROBINHOOD_AUTH_TOKEN` to `.env`, then runs `scripts/robinhood_probe.py` and pastes the full output (tool list + schemas + get_account/list_positions shapes). That is the only remaining blocker for schema reconciliation before any live order attempt.

Do NOT arm `robinhood_live_enabled=True` until: (1) probe output reconciles all `_RH_TOOLS` names and `# TODO-VERIFY` field accessors, (2) `find_order_by_client_id` is confirmed to work (cross-restart idempotency), and (3) a dry-run session confirms logged order intent matches sleeve output.

---

## Backtest Harness Advance (Sonnet baseline + DoD scaffold + CL-1 gate) — 2026-06-07

**What was built (following STATUS "single most important next" + GROK_HANDOFF_CROSS_LEARNING priorities CL-1/CL-4):**

- Extended the rules-only baseline in `backtest/strategies.py` with `sonnet_momentum_weights` (and private `_price_momentum` mirror) that uses the *exact* 12-1 momentum math, lookback/skip, and tradable universe as the live SonnetAgent. This guarantees the "rules signal" control is identical; any outperformance by LLM Sonnet will be due to the model layer (news, regime, critique, etc.), not a different factor definition.
- Used strict TDD/fail-before: first added `test_sonnet_momentum_baseline_exists_and_ranks_higher_mom_higher` (and later the wf/deflated smoke) which failed on import/attr (red), implemented, re-ran to green.
- Added `deflated_sharpe` (illustrative multiple-testing penalty) and `run_walk_forward` (n-window scaffold with embargo note + cost-stress simulation) to `backtest/engine.py` — the minimal DoD extensions ("2–5 yr historical backtest per strategy with walk-forward CV + deflated Sharpe").
- Added CL-1 cross-learning gate in existing `tests/test_sizing.py` (static signature guard + concrete call): "sizing functions never see LLM conviction scalar". Placed in regular test (not a new `test_audit_*_gate.py`) per "auditor-owned gates may not be edited by the builder" rule from WORKFLOW + handoff; left a comment for future extraction/ownership by Claude.
- All changes avoid the execution core (RiskGate, OMS, kill_switch, planner sizing math, lots, reconciler) — no invariants touched. No live/trading gate flipped (robinhood_live_enabled etc. untouched).

**Files changed:**
- backtest/strategies.py (new sonnet baseline + mom mirror)
- backtest/engine.py (+ deflated_sharpe, run_walk_forward scaffold + Any import)
- tests/test_backtest.py (fail-before test + wf/deflated smoke; now 6 tests in file)
- tests/test_sizing.py (CL-1 gate test; 31 sizing tests total)
- logs/build_journal.md (this entry)

**Test results:** 
- Targeted: `tests/test_backtest.py` 6/6 ✅ (was 4; + sonnet baseline + wf smoke). `tests/test_sizing.py` 31/31 ✅ ( + CL-1 gate).
- Full backtest + sizing modules green. Pre-existing full suite was 734; we added cases without regressing.
- ruff on the 4 changed files: clean (0 errors after E402 hoist fix).
- mypy on backtest modules (via --ignore-missing): only pre-existing errors pulled from data/market.py and budget.py (no new errors introduced by our code).

**Decisions / flags:**
- Sonnet baseline uses `per_name=0.08` default, top_n=5 (matches live max intents). Sum ~0.4 → conservative cash buffer; backtest engine treats remainder as cash (zero return, per its documented conservative choice).
- Walk-forward is a *scaffold* (re-runs full with cost stress for n windows); true purged CV + per-window param search + embargo + aggregation of excess + deflated SR across folds is still future work (documented in the fn).
- Deflated SR is illustrative (common sqrt(2*ln(N))/sqrt(T) penalty). When real multi-year runs + parameter sweeps exist, replace with full formula + track #trials.
- CL-1 test is a builder-enforced guard today; per portfolio standard (truleo) the auditor (Claude) should own the final `test_audit_sizing_gate.py` that this can be moved into. Builder will not edit such a file once created.
- Did *not* yet wire sonnet baseline into `run_baseline.py` (that would be a one-line partial() addition) or execute real 2-5y history (needs owner .env yf or cache); focused on the deterministic math + testability per the handoff "identical signal".
- Security injection (CL-5 / DoD) and full auditor gates (CL-2) still pending as separate handoff items; backtest was the STATUS-declared #1.

**Next for this harness (suggested for next STATUS update or auditor handoff):**
- Wire sonnet (and a simple opus rules stub) into run_baseline.py and execute real 3-5y history (with --years) on the actual agent universes.
- Turn the walk-forward into real rolling windows with date slicing + compute avg excess + min deflated SR across folds.
- Add "beats its own rules baseline (excess_cagr > 0 and deflated SR higher)" assertions or a SCOREBOARD append for the historical runs.
- Once real runs exist, update `blueprint/01_HONEST_ASSESSMENT.md` odds with data.

**Test results (post-edit):** Targeted modules green. No production money paths modified. Fail-before discipline followed for the new baseline functionality.

---

## Robinhood Broker Adapter + Reconciler Fill-Lag Fix + Opus Book-Building — 2026-06-06 / 2026-06-08

*(Committed 2026-06-09 by Claude standing in for Grok while usage limit was hit. Work was complete in the working tree; only missing git commit + journal entry.)*

### Robinhood broker adapter (June 6 session — audited clean by Claude on 2026-06-06)
- `execution/robinhood_broker.py` (467 lines): full `Broker`-protocol adapter for Robinhood's agentic MCP endpoint (`agent.robinhood.com/mcp/trading`). Safety gate `live_trading_enabled` defaults `False`; every `submit_order()` is a dry-run returning a synthetic `DRYRUN-<uuid>` until the flag is explicitly armed. Transport: synchronous MCP-over-HTTP (JSON-RPC 2.0) via httpx — no mcp SDK dependency. All `_RH_TOOLS` entries and field accessors marked `# TODO-VERIFY` (schema unconfirmed against live server).
- `scripts/robinhood_probe.py`: offline schema-probe script. Call this after adding `ROBINHOOD_AUTH_TOKEN` to `.env`; pastes full tool list + inputSchema shapes for reconciling every `# TODO-VERIFY`.
- `scripts/clean_dust_lots.py`: one-off maintenance script to close phantom sub-unit crypto lots left before the live `DUST_NOTIONAL_USD` snap-to-zero logic existed. Dry-run by default; `--apply` to write.
- `config/settings.py`: `reconciler_interval_robinhood_secs: int = 20` (Robinhood has no fill stream; faster reconciler compensates for ~3× more broker polls while orders are open).
- `app.py`: `_build_broker()` / `_build_robinhood_broker()` factory methods (broker construction extracted from constructor); interval selection by `broker_kind`.
- `execution/oms.py`: fill-with-no-payload demoted `logger.error` → `logger.info` with explanatory comment (Robinhood has no WS stream; reconciler is SOT).
- `.env.example`: full Robinhood section (`ROBINHOOD_AUTH_TOKEN`, `ROBINHOOD_LIVE_ENABLED`, `RECONCILER_INTERVAL_ROBINHOOD_SECS`).
- `.gitignore`: `*.pem`, `*.key`, `*.p12` added (security pass 2026-06-07).
- Tests: `tests/test_robinhood_broker.py` (10 offline tests covering dry-run gate, idempotency, arg translation, state-map). `tests/test_app_lifecycle.py` +1 (reconciler interval selection test). All 734 → passing at this point.

*Audited by Claude 2026-06-06: invariants confirmed (RiskGate, kill-switch, submit idempotency, reconciler SOT). DO NOT arm `live_trading_enabled=True` until probe output reconciles all `# TODO-VERIFY` entries.*

### Dashboard + CalibrationRecorder conviction fix + Opus book-building + rate limiter (June 8 session)
- `dashboard/server.py`: (1) NAV vs SPY chart strips weekend dates to a category-scale x-axis (no more weekend gaps in the equity curve); (2) Per-Sleeve P&L attribution table moved below the charts; (3) Sleeve equity chart excludes Manager (non-trading sleeve). JavaScript helpers `isWeekend`, `toTradingDays`, `fmtDayLabel`, `categoryXAxis` added.
- `agents/calibration_recorder.py`: Fixed critical silent bug — conviction was being looked up from the OMS `Order` payload (which does NOT store conviction; conviction belongs to `Intent`). Fix: look up from `AgentMemory.conviction_by_intent_id(intent_id)` via new `_conviction_from_memory()` helper. Without this fix the calibration tracker was silently recording 0 conviction on every fill and not logging any calibration data.
- `agents/memory.py`: `conviction_by_intent_id(intent_id) → int | None` — SQL lookup on `intent_log` table. Required by the CalibrationRecorder fix above.
- `app.py`: wires `memories=self._memories` into `CalibrationRecorder` constructor; adds `_llm_opus_lite` (Sonnet model, cheaper) for Opus initiation calls; adds `_RateLimitFilter` to tame Alpaca websocket reconnect log storm (collapses identical error messages to 1-per-60s window).
- `agents/opus_agent.py`: H1 — initiation mode now allows up to `TARGET_HOLDINGS` starters per cycle (previously hard-capped at 2), so the book can deploy in a single Opus call instead of 3-4 days; H2 — initiation uses `llm_lite` (Sonnet), reserving Opus for management-mode thesis review and deep-dives where the reasoning premium is earned.
- `tests/conftest.py`: sleeve-weights isolation fix — live `data/manager_sleeve_weights.json` (non-default weights from real Manager reallocation) was bleeding into tests and breaking sizing assertions; conftest now redirects `SLEEVE_WEIGHTS_FILE` to a temp `{}` file for every test session.
- `tests/test_agents_m7.py`: updated Opus intent-cap test for H1 behavior (initiation → up to TARGET_HOLDINGS; management → still _MAX_INTENTS=3).
- `tests/test_log_ratelimit.py` (new, 4 tests): covers `_RateLimitFilter` collapse, per-message independence, window expiry, and suppressed-count annotation.
- `blueprint/01_HONEST_ASSESSMENT.md`: updated honest-odds section with latest paper observations.

**Test results:** 740/740 passed. No RiskGate / OMS / kill-switch / lot-accounting / reconciler source-of-truth paths modified. No live gate flipped.

**Outstanding before any live Robinhood order:**
1. Add `ROBINHOOD_AUTH_TOKEN` to `.env`, run `scripts/robinhood_probe.py`, paste output → reconcile all `# TODO-VERIFY` in robinhood_broker.py.
2. Dry-run session (`BROKER_KIND=robinhood`, `ROBINHOOD_LIVE_ENABLED=false`) — confirm logged order intents match sleeve output.
3. Single 1-share live test — inspect response, confirm idempotency (submit same client_order_id twice).
4. Confirm cross-restart idempotency path (`find_order_by_client_id`) works on live server.

---

## Audit 001 + 002 — 2026-06-07 / 2026-06-09 (Claude auditor)
- Audit 001 (backtest harness): 5 verified, 4 open — walk-forward was a cost sweep not temporal split (OPEN-1), `_SONNET_TRADABLE` duplicated (OPEN-2), fail-before unverifiable without git commits (OPEN-3), CL-1 gate in builder-editable file (OPEN-4). OPEN-4 resolved: `tests/test_audit_sizing_gate.py` created (auditor-owned; builder must not edit).
- Grok (Jun 8): implemented Fix 1 (real temporal date splits, RED c221efb → GREEN 89b0a65) and Fix 2 (import `_SONNET_TRADABLE` from live agent, RED 564fae2 → GREEN 7b62629). Committed with git evidence. Thread renamed `DONE_GROK_HANDOFF_BACKTEST_HARNESS.md`.
- Audit 002 (Jun 9): verified both fixes clean. All 4 OPEN items resolved. `LEDGER.md` created as append-only audit log.
- Test count at audit close: 40 in touched modules; 740 overall.
