# Multi-Agent Asset Competitive Bot

Three Claude models — **Haiku 4.5, Sonnet 4.6, Opus 4.7** — each manage a $1,000 paper portfolio through Alpaca. A 4th Manager agent allocates capital and enforces risk. Goal: outpace SPY net of taxes and net of API costs. Hard cap: $1.00/day on Anthropic spend during development.

Local "Bloomberg-lite" terminal at `http://localhost:8081`.

## What's in this folder right now

```
research/
  01_theaiportfolios.md         — research on @theaiportfolios on X (closest live analog)
  02_institutional_strategies.md — quant + sell-side strategies, ranked for our use case
  03_autotrader_frameworks.md   — backtest libs, OMS patterns, dashboards, full ref architecture
  04_alpaca_and_budget.md       — Alpaca capabilities + verified $1/day budget math
  05_sdk_capabilities.md        — alpaca-py 0.43.2 MLEG support + Batch API tool use (both GO)
  06_vectorbt_benchmark.md      — proxy benchmark, verdict VIABLE for our 5yr × 500-stock harness
  06_vectorbt_benchmark.py      — runnable script if you want to confirm on your own Mac
  07_leverage_strategies.md     — institutional leverage frameworks + per-agent recommendations

blueprint/
  00_BLUEPRINT.md                — the architecture for the system (v0.3, current)
  01_HONEST_ASSESSMENT.md        — candid take on what's sound, what's wrong, what's risky
  CHANGELOG.md                   — what changed across blueprint versions
  prompts/                       — v1 system prompts for all 4 agents

src/                             — empty; code goes here after blueprint sign-off
data/                            — DuckDB / Parquet / SQLite (gitignored)
logs/                            — daily journals, agent memos, intent logs
```

## Read order

1. `blueprint/01_HONEST_ASSESSMENT.md` — start here, 5-minute read
2. `blueprint/00_BLUEPRINT.md` — the architecture, 15-minute read, ends with 5 questions for you
3. `research/04_alpaca_and_budget.md` — if you want to verify the $1/day math
4. `research/01_theaiportfolios.md` — if you want context on the closest live analog
5. `research/02_institutional_strategies.md` and `research/03_autotrader_frameworks.md` — reference material; skim sections of interest

## Status

- Research: complete (7 files)
- Blueprint v0.4: current. Adds full professional leverage system (per-agent caps, vol-targeting, drawdown ladder, VIX ladder, LETF + options policy, MC slider math), locks free-only data stack (no paid subs), simplifies process mgmt to manual `python app.py`.
- Agent prompts v1.1: leverage paragraphs added to all 4 in `blueprint/prompts/`. Manager gains a `mc_proposal.json` call type.
- Code: not yet written — milestone 1 next session

## Next step

Build order is in `blueprint/00_BLUEPRINT.md` §12. Eight milestones, each runnable:
1. Skeleton & types
2. OMS with FakeBroker (crash-recovery test)
3. RiskGate, sizing, kill switches, lot ledger, wash-sale
4. AlpacaBroker adapter + paper credentials
5. MarketData + DuckDB + News adapters
6. Haiku end-to-end
7. Sonnet + Opus (w/ deep-dive cron) + Manager
8. Dashboard (read-only, decoupled)
