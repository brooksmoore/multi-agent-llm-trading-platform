# Claude Code Handoff Prompt

> Paste this entire file into your first Claude Code session in `~/Desktop/Multi_Agent_Asset_Competitive_Bot`.
> Recommended model: **Claude Sonnet 4.6** for the build. Switch to **Opus 4.7** for milestones 2 (OMS + crash recovery) and 8 (final integration review).

---

## Your assignment

You are taking over implementation of the **Multi-Agent Asset Competitive Bot** — a four-agent (Haiku 4.5, Sonnet 4.6, Opus 4.7, Manager) paper-trading system on Alpaca, with a local "Bloomberg-lite" terminal at `localhost:8081`. Hard cap: $1.00/day on Anthropic API spend during development. Goal: outpace SPY net of taxes and net of API costs.

A previous Claude session did the entire research and architecture phase. **All decisions are already made and documented.** Do not re-litigate the architecture. Read the docs, then build.

## Read these in order before writing any code

1. `README.md` — project overview, current status, folder layout
2. `blueprint/00_BLUEPRINT.md` — the complete architecture (this is your spec)
3. `blueprint/01_HONEST_ASSESSMENT.md` — risks and what's most likely to go wrong
4. `blueprint/CHANGELOG.md` — what changed across v0.1 → v0.4 and why (don't undo locked decisions)
5. `blueprint/prompts/*.md` — the four agent prompts (already drafted, drop into `agents/prompts/`)
6. `research/04_alpaca_and_budget.md` — verified budget math and Alpaca SDK details
7. `research/05_sdk_capabilities.md` — alpaca-py 0.43.2 multi-leg + Batch API tool use specifics
8. `research/07_leverage_strategies.md` — institutional leverage system you must implement faithfully
9. Skim `research/02_institutional_strategies.md`, `research/03_autotrader_frameworks.md`, `research/06_vectorbt_benchmark.md` for context as needed

After reading, summarize back to me in ≤200 words: (a) what you understand the system to be, (b) what you're going to build first, (c) any genuine ambiguity in the blueprint that needs my input before you start. Do this BEFORE touching a file.

## Build order (from blueprint §12)

Each milestone ends in a runnable, demoable system. Do not skip ahead. Each milestone gets its own git commit.

1. **Skeleton & types** — `pyproject.toml`, `core/`, `EventBus`, `Intent/Order/Fill/Lot` dataclasses, FSM helper. Tests pass on the FSM.
2. **OMS with `FakeBroker`** ⚠️ *use Opus 4.7 for this milestone* — submit-an-order lifecycle works end-to-end with no real broker. `test_oms_recovery.py` proves crash-mid-trade is non-fatal. This is the highest-stakes module; do not move on until tests are exhaustive.
3. **RiskGate, sizing, kill switches, lot ledger, wash-sale, leverage caps** — all deterministic guards in place with full test coverage. Implement the leverage system from blueprint §16 *exactly*: per-agent `base_max_gross`, EWMA λ=0.94 vol-targeting with 8% floor and 1.75× cap, drawdown ladder, VIX ladder, LETF 5-day max-hold, options whitelist. No LLM, no broker yet.
4. **AlpacaBroker adapter + paper credentials** — `alpaca-py 0.43.2` (verify the version on PyPI when you start). Smoke test: hardcoded "buy 1 share of SPY" round-trip through OMS → RiskGate → Alpaca → fill → reconcile → ledger updated.
5. **MarketData (live + replay) + DuckDB + News adapters** — Alpaca websocket, DuckDB+Parquet store, free news adapters per blueprint §17 (EDGAR, Finnhub free, yfinance, RSS, FRED). One week of cached bars locally.
6. **One LLM agent end-to-end** — Haiku with the GTAA mandate (`agents/prompts/haiku_agent.md`), real prompts, full budget enforcement (the `daily_spend.json` ledger refusing calls > $0.95/day), paper trade through the full stack. Daily budget < $0.10 for this one agent alone.
7. **Sonnet, Opus (with Thu/Fri deep-dive cron), and Manager agents** — all four wired in, sleeve allocation working, weekly leaderboard report generated. Reactive Haiku news scans on volatility-spike triggers. Tax-aware behaviors active. `MASTER_CAPABILITY` slider integrated into sizing.
8. **Dashboard last** ⚠️ *use Opus 4.7 for the integration review at the end of this milestone* — Plotly Dash on `:8081`, all panes per blueprint §11, polls SQLite/DuckDB every 3s. Read-only. Decoupled from the bot loop entirely.

## Non-negotiable rules

These are derived from research file 01 (the @theaiportfolios analog and the $441K Lobstar-Wilde blow-up case) and from blueprint §1.

1. **The LLM never sizes positions, never computes costs, never decides max bet.** All of that is deterministic Python. The LLM proposes a target weight; everything else is enforced.
2. **Every Anthropic call must be cached** (`ttl: "1h"` explicitly — Anthropic silently regressed the default to 5m). System prompt + tool definitions + portfolio state in the cached prefix; only the per-call market summary and question as fresh tokens.
3. **Hard `max_tokens` ceilings**: Haiku 256–1024, Sonnet 1024–2048, Opus 2048–8192 (8K only on scheduled deep-dives). Output is 5× input cost.
4. **`daily_spend.json` ledger is mandatory.** The LLM wrapper refuses any call where `today_spent + estimated_cost > $0.95`. Degrade to Haiku-only mode rather than overspend.
5. **Append-only event log for the OMS.** Every Intent → Order → Fill state transition persisted to SQLite WAL *before* the side-effect.
6. **Reconcile against Alpaca every 60 seconds.** A 1-share or $1 mismatch → `RECONCILIATION_BREAK` halt + alert.
7. **Hexagonal architecture.** `Broker`, `MarketData`, `LLM`, `News` are interfaces. Adapters at the edges. Core depends only on interfaces. This is what makes `backtest == paper == live` true.
8. **`AUTO_APPROVE = true`** by default in dev (user wants full autonomy on paper). The approval queue exists as a code path behind the feature flag.
9. **`MASTER_CAPABILITY = 1.0`** default, slider on the dashboard, range [0.0, 1.5]. Above 1.5 requires `OVERRIDE_KEY` env var.

## Stack (locked, do not change)

- Python 3.12, `uv` for env, `ruff`, `mypy --strict` on `agents/`, `execution/`, `core/`
- `alpaca-py` (verify version 0.43.x at install time)
- `anthropic` async client with budget wrapper
- `vectorbt` for rules-only backtests (verified VIABLE on M-series Macs in research file 06)
- DuckDB + Parquet (market/news), SQLite WAL (OMS state, lots, agent memory)
- `diskcache` for HTTP responses
- `APScheduler` for cron jobs
- FastAPI + Plotly Dash (dashboard)
- `pytest` + `FakeBroker` for unit tests
- `ntfy.sh` for alerts (Telegram adapter stubbed for v1.5)

## Process

- One git commit per milestone, conventional-commits style.
- Run `ruff check` and `mypy --strict` before committing.
- After each milestone, write a short milestone-end note to `logs/build_journal.md` with what was built, what surprised you, and what's pending.
- If you hit ambiguity in the blueprint that genuinely blocks you, **stop and ask Brooks** rather than guessing. Document the question in the build journal.
- If you discover a research file claim is wrong (SDK version moved, API shape changed, etc.), update the relevant research file with a `> [VERIFIED 2026-MM-DD by Claude Code]` annotation and proceed.

## Things you should NOT do

- Do not add features beyond the blueprint without flagging them first.
- Do not silently switch libraries (e.g., from Plotly Dash to Streamlit) — these were chosen deliberately.
- Do not skip tests on `core/`, `execution/`, or `agents/llm.py`. These three modules are where bugs become catastrophic.
- Do not change the leverage system math from blueprint §16. Brooks asked for institutional-grade leverage logic; the parameters are derived from research file 07 with citations. Do not "improve" them by intuition.
- Do not enable real-money trading in any code path. Alpaca paper credentials only. Real-money graduation is a 6-week-out decision per blueprint §13.
- Do not add public-facing endpoints. Everything is local-only per Brooks's privacy requirement.

## When you're done with milestone 8

Run the system end-to-end for one full trading day. Then write a final report to `logs/v1_complete.md` covering:
- Total LOC, test coverage %, milestones completed
- Daily API spend observed vs. budgeted
- Any deviations from the blueprint and why
- Open issues for v1.5 (Telegram integration, optional Finnhub paid upgrade decision, etc.)
- A "first 6 weeks of paper" checklist for Brooks to monitor

Then stop. Real-money deployment is gated on the §13 graduation criteria and is not your call.

## Now

Read the docs in the order listed at the top. Summarize back to me in ≤200 words. Then start milestone 1.
