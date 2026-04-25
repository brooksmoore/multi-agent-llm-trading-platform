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
