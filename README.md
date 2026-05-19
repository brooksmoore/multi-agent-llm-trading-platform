# Multi-Agent LLM Trading Platform

A paper-trading platform where four Claude models run differentiated investment strategies on Alpaca, with a deterministic Python guardrail layer between LLM judgment and irreversible action.

Three Claude models — **Haiku 4.5, Sonnet 4.6, Opus 4.7** — each manage a $1,000 paper portfolio through Alpaca. A fourth **Manager** agent allocates capital between sleeves, oversees portfolio-level risk, runs a weekly regime read, and writes adversarial critiques of high-conviction trades. Goal: outpace SPY net of taxes and net of API costs. Hard cap: $1.00/day on Anthropic spend.

Local "Bloomberg-lite" terminal at `http://localhost:8081`.

## Status

Active development. Core platform is built and runs end-to-end against an Alpaca paper account. The Python guardrail layer, OMS with crash-recovery replay, broker reconciliation, kill-switch engine, and 41-file pytest suite are all in place. Live paper trading is in progress; backtesting harness and weekly performance reporting are next.

## Architecture

The architectural commitment that makes the system safe: **LLMs propose target weights only; every dollar amount is computed in Python, and every trade clears a deterministic risk layer before reaching the broker.**

- **Hexagonal architecture.** `Broker`, `MarketData`, `LLM`, `News` are interfaces. Adapters for Alpaca, Anthropic, EDGAR, Finnhub live at the edges. The core depends only on interfaces, so backtest, paper, and live execution share one code path.
- **Append-only OMS event log.** Every `Intent → Order → Fill` state transition is persisted to SQLite WAL before the side effect. On crash, replay the log and reconcile against the broker.
- **60-second broker reconciliation.** The broker is the source of truth. Any local-vs-broker mismatch flips the system to `RECONCILIATION_BREAK` and halts new orders.
- **Pre-trade RiskGate.** Every intent passes through a single Python function enforcing position limits, sector caps, leveraged-ETF holding rules, drawdown ladders, wash-sale windows, and a daily API budget. No LLM ever bypasses it.
- **Per-agent budget ledger.** Cost-controlled API spend, capped at $1.00/day across all four agents. The LLM wrapper refuses any call that would exceed the cap and degrades to Haiku-only mode rather than overspend.
- **Skip-when-unchanged signal gating.** If an agent's inputs haven't changed since the last tick, no LLM call fires. Cuts API cost on quiet markets.

## Agent mandates

- **Haiku** — trend-following on a 10-ETF universe (Faber GTAA) plus a 24/7 crypto sleeve on BTC/ETH/SOL. Monthly rebalance.
- **Sonnet** — multi-factor scoring (value + momentum + quality) on liquid US large/mid caps. 10–15 names, monthly rebalance.
- **Opus** — concentrated discretionary, GARP-style. 5–8 names with explicit bull/bear theses. Scheduled deep-dives twice a week ingest ~150K tokens of context (10-Qs, earnings releases, insider activity) per holding.
- **Manager** — CIO-level: 4-week Sortino-based capital reallocation, portfolio-level risk oversight, weekly regime read, adversarial critique of high-conviction trades, weekly leaderboard journal.

## Repository

```
core/        — types, events, FSM, clock
agents/      — Haiku, Sonnet, Opus, Manager + LLM wrapper, memory, calibration
execution/   — OMS, broker adapters (Alpaca + FakeBroker), risk gate, reconciler, kill switch, lots
data/        — Alpaca + yfinance market data, news adapters (EDGAR / Finnhub / RSS), summarization
ops/         — alerts, heartbeat, journal, Telegram adapter
dashboard/   — Plotly Dash terminal on :8081
tests/       — 41-file pytest suite
research/    — 7 research files informing the architecture
blueprint/   — 600-line architecture spec + honest assessment + agent prompts
```

## Tech

Python 3.12, Anthropic API, alpaca-py, SQLite (WAL), APScheduler, FastAPI, Plotly Dash, vectorbt (for rules-only baselines), pytest, mypy --strict.

## Running

```bash
uv run python app.py
```

Requires `.env` with Anthropic and Alpaca paper credentials. See `.env.example`.

## Reading order

If you're new to the repo, start here:

1. `blueprint/01_HONEST_ASSESSMENT.md` — candid take on what's sound, what's wrong, what's risky (5-minute read)
2. `blueprint/00_BLUEPRINT.md` — the full architecture (15-minute read)
3. `app.py` — the orchestrator that wires everything together

## Contact

Brooks C. Moore · brcmoore@umich.edu · [linkedin.com/in/brooks-moore](https://linkedin.com/in/brooks-moore)
