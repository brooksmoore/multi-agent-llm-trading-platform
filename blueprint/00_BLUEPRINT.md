# Multi-Agent Asset Competitive Bot — Architecture Blueprint

**Version:** 0.4 (adds professional leverage system from research file 07)
**Date:** 2026-04-25 (revised)
**Author:** Synthesized from research files 01–07 + user decisions
**Status:** Approved direction; agent prompts drafted; implementation begins next

> Change history lives in `CHANGELOG.md` next door.
> Agent prompts live in `prompts/` next door.

---

## 0. Project mission, restated

Three Claude models — **Haiku 4.5, Sonnet 4.6, Opus 4.7** — each manage a **$1,000 paper-trading portfolio** through Alpaca. A fourth **Manager** agent sets capital allocation, enforces risk caps, runs a regime read, and writes the unified weekly report. The aggregate $3,000 portfolio's goal is to **consistently outpace SPY net of taxes and net of API costs**, on a daily/weekly cadence.

Hard constraints, locked:

1. Anthropic API spend ≤ **$1.00 / day** during development.
2. Broker = **Alpaca paper** (free, official, supports stocks + options + crypto).
3. Local "Bloomberg-lite" terminal at **`http://localhost:8081`**, polling every 3s.
4. **Full autonomy from day 1** in paper. The approval queue is a code path that exists behind a feature flag (`AUTO_APPROVE = true` by default in dev). The bot is built as if managing real money, but no real money is at risk.
5. **Everything private.** Local files only. No public Substack.
6. **Telegram notifications** are a confirmed v1.5 feature (post-milestone-7). Adapter stub at `ops/telegram.py` from milestone 1.
7. **Master leverage lever** `MASTER_CAPABILITY ∈ [0.0, 1.5]` (default 1.0). It is a *joint multiplier on each agent's per-strategy leverage cap and vol-target* — not a flat weight scalar. See §17 for full math. Dashboard slider. 0.0 = read-only mode (memos still produced; nothing executes). 1.0 = institutional-default sizing per agent. 1.5 = aggressive overrange (Haiku approaches Reg-T's 2× ceiling, which Python hard-clips). Values >1.5 require an `OVERRIDE_KEY` env var.

What is explicitly *not* the goal:

- HFT / scalping / sub-second edge — LLMs can't win this and trying will burn the budget.
- "100% annual returns safely" — retired.
- Real-money deployment in week 1.

---

## 1. Architectural principles (non-negotiable)

1. **Hexagonal architecture.** `Broker`, `MarketData`, `LLM`, `News` are interfaces. Adapters for Alpaca / Anthropic / Finnhub / EDGAR live at the edges. The core (agents, OMS, risk) depends only on the interfaces. This is what makes `backtest == paper == live` true.
2. **Intents, not actions.** Agents emit declarative `TargetPosition` intents. A separate `ExecutionPlanner` decides how / whether / when to execute. Agents never call the broker directly.
3. **Single chokepoint for risk.** Every intent passes through one `RiskGate`. Hard-coded Python rules — never the LLM — decide max position size, sector caps, PDT count, kill-switch state, wash-sale window, long-vs-short-term tax preference. *This is the rule that prevents Lobstar-Wilde-style $441K blow-ups.*
4. **Broker is the source of truth.** Local state is a derived cache, reconciled against Alpaca every 60 seconds. A 1-share or $1 mismatch flips the system to `RECONCILIATION_BREAK`.
5. **Append-only event log for the OMS.** Every `Intent → Order → Fill` state transition is persisted to SQLite WAL *before* the side-effect. On crash, replay the log and reconcile.
6. **Cache or die.** No Anthropic call > 2K input tokens may be uncached. The shared system prompt + tool definitions + portfolio state live in a 1h-TTL cache; we always pass `ttl: "1h"` explicitly because Anthropic silently regressed the default to 5m in early 2026.
7. **Hard `max_tokens` ceilings.** Haiku 256–1024, Sonnet 1024–2048, Opus 2048–8192 (8K only on the scheduled deep-dives). Output tokens are 5× input cost — capping output is the single biggest budget lever.
8. **Public-grade journal, locally only.** Every decision (including rejected) logged in Markdown. No selective publication. Calibration only happens if losers are tracked. Files live in `logs/` and are read by you and Claude in conversation.
9. **The dashboard is read-only.** It polls SQLite/DuckDB every 3s; it never mutates state and is not on the trading code path. A dashboard bug cannot affect a trade.
10. **Lot-level accounting from day 1.** Alpaca's API does not return per-lot cost basis; we maintain our own ledger in SQLite. This is required for the wash-sale checker, the long-vs-short-term tax preference, and per-agent attribution.
11. **The LLM never sizes positions or computes costs.** Sizing module in Python; cost-basis ledger in Python; tax math in Python. The LLM proposes a target weight and a thesis; everything else is deterministic.

---

## 2. Cadence and lifecycle

| Cadence | What happens | Who runs |
|---|---|---|
| **Daily, 09:25 ET** (pre-open) | Brief: market context, watchlist, overnight news, agent state read. *Plan* changes for the day. | Sonnet most days; Manager-agent does a regime read once/week |
| **Daily, intraday checkpoints** (10:30, 12:00, 15:00 ET) | Re-evaluate open theses, react to news, propose intraday adjustments only if a hard catalyst hit | Sonnet |
| **Daily, 13:30 + 15:55 ET** | Lightweight news/sentiment scans | Haiku |
| **Daily, 16:30 ET** | EOD review: P&L attribution, calibration check, journal generation | Sonnet via Batch API (50% off) |
| **Daily, 24/7** | Crypto sleeve monitoring (BTC/ETH/SOL trend) — only triggers an action on signal | Haiku |
| **Reactive (any time)** | Volatility-spike trigger: Python detects a >2σ move on a held name, a >1.5σ move on SPY/VIX, or a tagged macro event (Fed surprise, big earnings miss, geopolitical break). Triggers a Haiku "news scan" call (~$0.02). If Haiku's scan flags it as material, it can escalate to Sonnet (~$0.06) and/or alert the user via ntfy/Telegram. | Haiku → Sonnet escalation |
| **Weekly, Thursday 16:30 ET** | **Opus deep-dive #1** — one of Opus's holdings gets a full-context investment memo | Opus |
| **Weekly, Friday 16:30 ET** | **Opus deep-dive #2** — second holding gets the deep-dive treatment | Opus |
| **Weekly, Friday 17:00 ET** | Manager: regime read, adversarial critique of each agent's highest-conviction intent of the week, weekly leaderboard report | Manager (Sonnet) |
| **Every 4 weeks, end of week** | Manager re-allocates capital between sleeves based on rolling 4-week risk-adjusted return | Manager |
| **Monthly, 1st trading day** | Full portfolio rebalance per agent | All three |

**Default trade frequency: 1–4 trades per week per agent.** Anything more is a flag that something is off.

We start the paper account at **$30,000** (then mentally allocate $1K notional per agent for the leaderboard) so the PDT rule's $25K threshold doesn't hit even if an agent has a temporary day-trading week. Per-agent sleeves are tracked in our own ledger, not in Alpaca's account (Alpaca sees one account; we partition).

---

## 3. Agent mandates

### Haiku 4.5 — "The Trend Follower" (dual-mandate)

**Equity sleeve (~70% of Haiku's $1K, Mon–Fri):**
- Faber-style GTAA + cross-sectional ETF momentum.
- Universe: SPY, QQQ, IWM, EFA, EEM, TLT, IEF, GLD, USO, VNQ.
- Signal: 10-month SMA trend filter; rank by momentum within "in-trend" assets; equal-vol-weighted.
- Cadence: weekly check-in, monthly rebalance.

**Crypto sleeve (~30% of Haiku's $1K, 24/7):**
- Universe: BTC/USD, ETH/USD, SOL/USD via Alpaca crypto.
- Signal: 50-day SMA crossover + 14-day momentum filter.
- Cadence: continuous monitoring (cheap Python signals); LLM call only when signal flips.
- Fees acknowledged: 0.25% per side + spread. Sized so a bad crypto week ≤3% drag on aggregate.

**Why Haiku for both:** Rules-heavy decisions, low-token reasoning per call. Haiku is also the only agent positioned to react to weekend crypto news.
**Position sizing:** Vol-targeted, max 25% in any one ETF, max 12% in any one crypto. 10% cash buffer if fewer than 3 ETFs are in uptrend.

### Sonnet 4.6 — "The Multi-Factor Quant"
- Strategy: composite value + momentum + quality factor scoring on liquid US large/mid caps. 10–15 names, monthly rebalance.
- Universe: S&P 500 + selected Russell 1000 mid-caps (ADV > $20M).
- Signal: Z-score each name on (a) value (P/E, EV/EBITDA), (b) momentum (12-1), (c) quality (ROE, accruals); combined Z drives ranking.
- Cadence: daily monitoring, monthly rebalance, ad-hoc earnings/news exits.
- Position sizing: equal-weight or modest conviction-tilt. Single-name cap 12%.

### Opus 4.7 — "The Concentrated Discretionary PM"
- Strategy: GARP / sell-side discretionary — 5–8 high-conviction names with full bull/bear thesis, catalyst calendar, 10-Q reads.
- Universe: same liquid US large/mid universe, narrowed to ~30 candidates after the morning brief.
- Signal: adversarial bull/bear prompt (the @theaiportfolios pattern), conviction score 0–10, sized by conviction × vol-target.
- Cadence: **daily lightweight check-ins** (Opus reads its own cached prior memos, ~$0.03/call) + **two scheduled deep-dives per week** (Thursday + Friday), each ~$0.40, each rotating through one of Opus's holdings so every name gets a full-context memo every ~3 weeks.
- Deep-dive content: most-recent 10-Q + 10-K, last 4 earnings call transcripts (where free), top 3 competitor filings, 90 days of company news, sector context. ~150–200K tokens of context per deep dive.
- Position sizing: conviction-weighted, single-name cap 18%.
- **Honest caveat (added per user feedback):** The marginal value of 200K-token Opus context vs. 80K-token focused context is not benchmarked in this codebase yet; the "lost in the middle" effect is real. We will instrument deep-dive memo quality (peer-rated by Sonnet against the rules-only baseline) and adjust context size by week 3.

### Manager (Sonnet) — "The CIO" *(expanded per user feedback)*

The Manager does not pick stocks. It owns six jobs:

1. **Capital allocation** between the three sleeves. Default: equal $1,000 each. Reallocates **every 4 weeks** based on rolling 4-week risk-adjusted return (Sortino, not Sharpe — penalizes downside, not all volatility). Max ±25% reallocation in any 4-week step (i.e., a winning sleeve can grow from $1,000 to $1,250, not to $1,500 in one move).
2. **Risk overseer.** Watches portfolio-level beta, sector concentration, pairwise correlation between sleeves. Vetoes intents that would push past portfolio-level caps.
3. **Drawdown circuit breaker.** Aggregate -15% from peak → halve sizes; -25% → pause new entries; -33% → liquidate to cash and write a postmortem. Daily intraday loss limit of -2% remains as a "bug-detection" trip wire.
4. **Weekly regime read.** Friday 17:00 ET, Manager produces a 200-word "regime read" (risk-on / risk-off / transitioning, vol regime, rate regime, key macro events ahead). This text is injected into all three agents' shared cached context block for the following week. Lets agents coordinate without seeing each other's books.
5. **Adversarial critique.** Manager reads each agent's highest-conviction *new* intent of the week and writes a one-paragraph red-team. Agent receives the critique on its next call; if it still wants to proceed, it explains why in writing.
6. **Weekly leaderboard journal.** `WEEK_NN.md`: per-agent and aggregate P&L (gross and net of taxes and API costs), Sortino/Sharpe, max DD, attribution by sector, calibration check, what worked, what didn't.

---

## 4. Daily API budget — cycle math (verified)

Steady-state weekday at $1.00/day:

```
09:25 ET   Sonnet pre-open brief                       1 call    ~$0.06
10:30 ET   Sonnet mid-morning re-eval                  1 call    ~$0.06
12:00 ET   Sonnet midday position review               1 call    ~$0.06
13:30 ET   Haiku news scan                             1 call    ~$0.02
15:00 ET   Sonnet power-hour signal                    1 call    ~$0.06
15:55 ET   Haiku close-of-day check                    1 call    ~$0.02
16:30 ET   Sonnet EOD review (BATCH, 50% off)          1 call    ~$0.03
           Opus daily prior-memo skim (cached read)    1 call    ~$0.03
           Calibration analysis (Sonnet, weekly cron)  pro-rated ~$0.02
                                                       ─────────
                                                       Mon-Wed total ~$0.36
```

Budget allocation across the week:

| Day | Cycle cost | Opus deep-dive | Day total |
|---|---:|---:|---:|
| Mon | $0.36 | — | $0.36 |
| Tue | $0.36 | — | $0.36 |
| Wed | $0.36 | — | $0.36 |
| Thu | $0.36 | $0.40 (deep dive #1) | $0.76 |
| Fri | $0.36 | $0.40 (deep dive #2) + Manager regime + adversarial $0.10 | $0.86 |
| Sat | $0.05 (Haiku crypto check, only if signal) | — | $0.05 |
| Sun | $0.05 | — | $0.05 |
| **Week** | | | **~$2.80** |

That's **$2.80/week** vs. the $7/week cap = **40% utilization**, leaving substantial headroom for:
- **Reactive Haiku news scans** on volatility spikes (the primary use of headroom — see §2). Each scan ~$0.02; budget 5–10/week typical, more in volatile weeks.
- Sonnet escalations from those Haiku scans when warranted (~$0.06 each).
- Calibration deep-passes ($0.30, run monthly).
- A monthly LLM-driven baseline comparison (~$0.50, run via Batch API).

A `daily_spend.json` ledger tracks all of this; the LLM wrapper refuses any call where `today_spent + estimated_cost > $0.95`.

A `daily_spend.json` ledger is mandatory. The LLM wrapper refuses any call where `today_spent + estimated_cost > $0.95`. Degrade to Haiku-only mode rather than overspend.

**Cache strategy:** Per-agent 1h-TTL block (15–20K tokens: system prompt, agent mandate, tool definitions, current portfolio state, regime read). Per-call new input ≤3K tokens (deterministic Python summary of market snapshot + headlines). Output capped per §1.

**Crucial: every market context block hitting the LLM is summarized by deterministic Python first.** No raw orderbook, no full filings, no news article bodies hit Sonnet/Haiku. Opus's deep-dive calls are the only exception: they get full-text filings as input.

---

## 5. Risk system (the part that matters most)

### Layer 1 — Pre-trade RiskGate
A single function every intent passes through. Each check returns `(ok, reason)`:

- Position size ≤ agent's per-name cap.
- Sector exposure ≤ 30% (per-agent and aggregate).
- Aggregate beta vs. SPY ≤ 1.2.
- Symbol on tradable allowlist (no leveraged ETFs, no OTC, no microcaps with ADV < $20M).
- Min price $5; min ADV $20M.
- Buying-power check (broker AND local).
- Per-agent rate limit (max 5 orders / minute).
- PDT counter (block 4th day-trade in 5 days under $25K).
- Kill-switch state must be `OK`.
- Wash-sale check (block re-entry of a closed-loss position for 31 calendar days).
- **Long-term-gains preference**: if closing a winning lot held 11+ months would convert to long-term gains within ~30 days, RiskGate flags the intent and asks the agent to explicitly confirm the short-term close is intentional (with reason). Agent can still proceed; we just want it on record.

Every rejection logged with full context to the intent log; the agent receives the rejection reason on its next call.

### Layer 2 — Position sizing
Vol-targeted, hard-capped, computed in Python:

```
position_value = min(
    agent_cap_per_name * agent_equity,
    target_risk * agent_equity / max(realized_vol_30d, min_vol_floor)
)
```

The LLM proposes a *target weight*, never a dollar amount. Sizing module translates weight → shares.

### Layer 3 — Kill switches *(per user feedback: more lenient drawdown)*
- **Daily loss limit:** -2% intraday → cancel all open orders, freeze new intents until manual reset (kept; intraday -2% usually means a bug).
- **Drawdown halving:** -15% from peak → halve sizes.
- **Drawdown pause:** -25% from peak → pause new entries.
- **Drawdown liquidate:** -33% from peak → liquidate to cash, write postmortem.
- **Per-agent benching:** 5 consecutive losing trades → agent benched for 24h, capital moves to cash.
- **Heartbeat:** main loop must tick every 60s; missed → halt + ntfy push.
- **Reconciliation break:** any local-vs-broker mismatch > $1 → halt + alert.
- **Token-budget exhaustion:** day's API spend > $0.95 → degrade to Haiku-only mode.

### Layer 4 — Broker-side stops
For every position with a stop in its thesis, also place a **GTC stop order at Alpaca**. Belt and suspenders.

### Layer 5 — Master leverage + regime gates
Sizing is finalized through this stack (see §17 for the full leverage system):

```
final_cap = base_max_gross[agent]
          × MASTER_CAPABILITY           # user slider, 0.0–1.5
          × vix_scalar                   # VIX-regime ladder (5.5)
          × dd_scalar                    # drawdown ladder (5.4)
final_vol_target = base_vol_target[agent] × MASTER_CAPABILITY
position_value = vol_targeted_size(weight, realized_vol, final_vol_target)
position_value = min(position_value, final_cap × agent_equity)
```

The `MASTER_CAPABILITY` slider is your one-touch override for the entire system. Setting it to 0.0 from the dashboard immediately stops new orders without halting the bot, killing agents, or losing context. Regime and drawdown scalars apply on top, so even at MC=1.5 a 25% drawdown forces 0× sizing (forced cash).

### Layer 6 — Approval queue (feature flag)
Every intent that survives Layers 1–5 lands in a `pending_intents` queue visible in the terminal. Default in dev: `AUTO_APPROVE = true` (your preference). Flag exists for any agent / any time you want to flip to manual review.

---

## 6. Tax-aware behavior *(new section per user feedback)*

The bot will be **generally tax-aware** without claiming to be a tax advisor. Defaults configurable in `config/settings.py`; using 30% short-term / 15% long-term as starting assumptions.

What this means concretely:

1. **Lot-level accounting from day 1.** SQLite `lots` table; every fill creates a lot; every close consumes lot(s) FIFO by default (LIFO selectable per-agent). Required for everything below.
2. **Wash-sale checker** in RiskGate: blocks re-entry of a closed-loss position for 31 calendar days. Documents the proxy used (e.g., SPY closed → buy RSP allowed).
3. **Long-term-gains preference** in RiskGate: see Layer 1 above.
4. **Tax-loss harvesting in Manager's weekly job:** identify losers held >7 days, swap to non-correlated proxy if available. Documents every swap. Crypto excluded (different tax treatment + spread risk).
5. **Year-end optimization sweep** in the November and December weekly jobs: realize losers to offset winners, defer winners into next year where possible.
6. **Two leaderboards:** every weekly journal reports both *gross* and *as-if-net-of-tax* P&L per agent. Net is what matters; gross is what gets posted on Twitter (not in our case, but the discipline matters).
7. **Crypto separate bucket.** Crypto-to-crypto trades are taxable events; tracked separately because rates and rules differ (spot crypto gets capital-gains treatment in the US, but the wash-sale rule does *not* apply per current IRS guidance — we still avoid frequent re-entries because it's a strategic discipline thing, not a tax thing).

---

## 7. Backtesting harness *(simplified per user feedback)*

Two purposes: (a) validate each agent's underlying *rules* before paper deployment, (b) compare LLM-driven decisions against the same rules played deterministically.

1. **`vectorbt` parameter sweep** for the deterministic parts of each strategy (Faber SMA period, momentum lookback, factor weights). Walk-forward CV mandatory; deflated Sharpe (Bailey & López de Prado) reported alongside raw Sharpe. ≤3 tunable parameters per strategy.
2. **Live paper-vs-baseline comparison.** Instead of re-simulating LLM decisions historically (over-engineered), we run the rules-only baseline *in parallel* with the LLM-driven sleeve in paper. Same data, same risk gate, same execution. After 4 weeks we have a clean A/B: LLM_sleeve vs. baseline_sleeve. After 6 weeks we have a real comparison.

For each strategy and each sleeve, report:
- Total return, CAGR
- Sortino, Sharpe
- Max drawdown, drawdown duration, **Calmar**
- Win rate, avg win / avg loss, profit factor
- Beta vs. SPY
- Deflated Sharpe (vs. number of parameter combos tried)
- All-in cost: realistic slippage (far-touch + 1 bp + impact), Alpaca crypto fees

**Baselines a strategy must beat to deploy real money** *(per user feedback: LLM must beat baseline on drawdown, not necessarily total return)*:
- 60/40 SPY/AGG portfolio.
- Buy-and-hold the strategy's first picks.
- A naïve rules-only version of the same strategy *with no LLM input*. **The LLM-driven version must beat the rules-only version on max drawdown and drawdown duration**, even if total return is similar — that's the honest test of whether the LLM is adding value where it should (regime sensitivity).

---

## 8. Data layer

| Concern | Choice | Why |
|---|---|---|
| Market data (live) | Alpaca `StockDataStream` websocket (IEX feed, free) + REST poll every 60s for reconciliation | Hybrid pattern from research file 03 |
| Market data (historical) | DuckDB over Parquet files partitioned by `symbol/year/month` | Zero-config, columnar, scales |
| News | SEC EDGAR (free, primary for filings) + Finnhub free tier + Yahoo Finance via `yfinance` + RSS (Yahoo, MarketWatch, Seeking Alpha, Reuters) + FRED (macro) | Layered free-only stack. See §15 for the full source-by-source breakdown. |
| Caching | `diskcache` keyed by `(endpoint, params, date)` with TTLs by data type | Cuts broker calls and LLM tokens |
| OMS state | SQLite, WAL mode, single file | ACID, simple, perfect for orders/positions/intents/agent memos/lots |
| Agent memory | SQLite + daily Markdown journal per agent | Database for queries, Markdown for human + Claude reading |
| Secrets | `.env` outside repo + `pydantic-settings` | No keys in git, ever |

---

## 9. Stack (concrete picks)

- **Language:** Python 3.12, `uv` for env management.
- **Linting / typing:** `ruff`, `mypy --strict` on `agents/`, `execution/`, `core/`.
- **Broker SDK:** `alpaca-py`.
- **LLM SDK:** `anthropic` async client, wrapped with budget enforcement, retries on 529 overload, backoff, full prompt/response logging.
- **Backtest:** `vectorbt` (free) for rules-only sweeps; the LLM A/B happens in paper, not in simulation.
- **Storage:** DuckDB + Parquet (market/news), SQLite (OMS state, lots, agent memory).
- **Cache:** `diskcache`.
- **Scheduler:** `APScheduler` for daily/weekly jobs.
- **Web/API:** FastAPI (REST surface for dashboard data + webhooks).
- **Dashboard:** Plotly Dash with custom dark "terminal" CSS, polls every 3s.
- **Eventing:** in-process `EventBus` (50-line custom or `blinker`).
- **Alerting:** `ntfy.sh` for HALTED, RECONCILIATION_BREAK, missed heartbeat. Telegram adapter stub for later.
- **Testing:** `pytest` with a `FakeBroker` adapter.
- **Process management:** Foreground `python app.py` in a regular Terminal window. Start manually, stop with Ctrl-C. Disable Mac sleep (`caffeinate -dimsu` in another terminal, or System Settings → Battery → "Prevent your Mac from sleeping" while charging). This matches the user's existing autotrader workflow. No tmux, no launchd. Keep it simple.

---

## 10. Folder layout (target ~5,000 LOC including tests)

```
Multi_Agent_Asset_Competitive_Bot/
  pyproject.toml
  .env.example
  README.md
  app.py                       # entrypoint: bot loop + dashboard

  config/
    settings.py                # pydantic-settings, all knobs
    agents.yaml                # per-agent model, sleeve %, risk caps, prompt path
    universe.yaml              # tradable symbols, blocklist, sector tags
    schedules.yaml             # cron-like job spec
    tax.yaml                   # bracket assumptions, harvesting rules

  core/
    events.py                  # EventBus, Event dataclasses
    state_machine.py           # generic FSM helper
    clock.py                   # wall vs. backtest clock abstraction
    types.py                   # Intent, Order, Fill, Position, Lot, NewsItem, AgentMemo

  data/
    market.py                  # MarketData interface + AlpacaMarketData / ReplayMarketData
    news.py                    # Finnhub / EDGAR / RSS adapters → NewsItem
    store.py                   # DuckDB + Parquet
    cache.py                   # diskcache wrapper
    summarize.py               # deterministic-Python briefing summarizers (token cap enforcement)

  agents/
    base.py                    # Agent ABC: .observe(state) -> list[Intent]
    haiku_agent.py             # GTAA + crypto trend
    sonnet_agent.py            # multi-factor
    opus_agent.py              # GARP + scheduled deep-dives
    manager_agent.py           # allocator + regime + adversarial + journal
    prompts/                   # one .md per agent, version-controlled
    memory.py                  # SQLite-backed memory + daily journal
    llm.py                     # anthropic wrapper: budget, retries, caching, logging
    calibration.py             # conviction-vs-realized scoring (Brier-style)

  execution/
    risk.py                    # pre-trade RiskGate (all checks)
    sizing.py                  # vol-targeted sizing + caps
    oms.py                     # OMS, owns trade-lifecycle FSM
    broker.py                  # Broker ABC + AlpacaBroker + FakeBroker
    reconciler.py              # 60s reconciliation loop
    kill_switch.py             # global + per-agent halts, drawdown ladder
    approval_queue.py          # pending_intents inbox (UI bridge, feature-flagged)
    lots.py                    # lot-level cost basis ledger (FIFO/LIFO)
    tax.py                     # wash-sale check, harvesting candidate finder

  backtest/
    engine.py                  # vectorbt rules-only sweep harness
    metrics.py                 # Sharpe, Sortino, max DD, deflated Sharpe, Calmar

  dashboard/
    server.py                  # Dash app on :8081, polls every 3s
    layout.py                  # per-agent column grid + top bar
    components/                # equity_chart, positions_table, memos_feed, trade_log, approval_inbox, spend_gauge
    theme.css                  # the "terminal" look

  ops/
    alerts.py                  # ntfy adapter + Telegram stub
    heartbeat.py               # writes a tick every loop; watchdog reads it
    journal.py                 # daily and weekly Markdown report generator
    schedules.py               # APScheduler jobs

  tests/
    test_state_machine.py
    test_risk_gate.py
    test_oms_recovery.py       # crash mid-trade, restart, reconcile
    test_agents_offline.py     # agents against canned market data
    test_budget_enforcer.py
    test_lots_fifo.py
    test_wash_sale.py
    fixtures/

  research/                    # the 4 research files
  blueprint/                   # this file + CHANGELOG
  data/                        # DuckDB / Parquet / SQLite (gitignored)
  logs/                        # daily journals, agent memos, intent logs
```

---

## 11. Dashboard — the local Bloomberg *(layout revised per user feedback)*

`http://localhost:8081`, dark theme, monospace, dense.

**Top strip:** total NAV · day P&L (gross / net of est. tax) · day spend ($ / $1.00) · halted/live status · heartbeat age · approval-queue count · current regime tag · current VIX-bucket scalar · **MASTER_CAPABILITY slider (0.0–1.5)** with current value prominent · aggregate "Leverage Budget Used" gauge

**Per-agent column tiles** (in addition to equity / positions / memos / intents from before):
- `effective_max_gross` (live-computed from MC × VIX × DD scalars), realized gross, vol-target vs realized vol, drawdown bucket, "Leverage Budget Used" mini-gauge.

**Per-agent columns (3 columns + Manager column):**

Each column shows, top to bottom:
- Agent name + model + current sleeve $ + 4-week return
- Sparkline equity curve (vs. SPY)
- Current positions (compact table)
- Latest 3 memos with timestamps; click to expand into full prompt + response + market context at decision time + (if applicable) actual fill quality
- Latest 3 intents (color-coded by status)

**Manager column** additionally shows: regime read, adversarial critiques pending, capital-allocation history.

**Bottom strip:** trade log (last 50 fills) · intent log (last 50 incl. rejected with reason)

**Spend gauge:** separate small panel showing today's $ spent vs. $1.00 cap, by model, with end-of-day forecast.

**Approval queue (drawer, hidden when AUTO_APPROVE=true):** shows pending intents only when manual approval is enabled. Bulk approve / per-agent toggle.

---

## 12. Build order (8 milestones, each runnable)

1. **Skeleton & types.** `core/`, `pyproject.toml`, `.env.example`, `EventBus`, dataclasses, FSM helper. Tests pass on FSM.
2. **OMS with `FakeBroker`.** Submit-an-order lifecycle works with no real broker. `test_oms_recovery.py` proves crash-mid-trade is non-fatal.
3. **RiskGate, sizing, kill switches, lot ledger, wash-sale.** All deterministic guards in place with full test coverage. *No LLM, no broker yet.*
4. **AlpacaBroker adapter + paper credentials.** Smoke test: hardcoded "buy 1 share of SPY" round-trip through OMS → RiskGate → Alpaca → fill → reconcile → ledger updated.
5. **MarketData (live + replay) + DuckDB store + News adapters.** One week of cached bars locally. ReplayMarketData lets us run same code against historical data.
6. **One LLM agent end-to-end.** Haiku with the GTAA mandate, real prompts, full budget enforcement, paper trade through the full stack. Daily budget < $0.10.
7. **Sonnet, Opus (with deep-dive cron), and Manager agents.** All four agents wired in, sleeve allocation working, weekly leaderboard report generated. Tax-aware behaviors active.
8. **Dashboard last.** Plotly Dash on :8081, all panes populated from SQLite/DuckDB. Read-only. Decoupled from bot loop.

---

## 13. Graduation criteria (paper → real money)

These are set *now*, before any bias creeps in:

1. **6+ weeks of continuous paper operation** with no `RECONCILIATION_BREAK` and no kill-switch trips not caused by intentional drawdown limits.
2. **Aggregate net return > SPY** (after simulated short-term cap gains tax of ~30% and after API cost drag) over the 6+ weeks.
3. **Aggregate Sortino > SPY's Sortino** over the period.
4. **Calibration check passes:** "9/10 conviction" trades right materially more often than "5/10 conviction." If not, the conviction signal is noise.
5. **At least one of the three sleeves beats its rules-only baseline** on max drawdown and drawdown duration — the LLM is adding value where it should.
6. **Weekly journal `WEEK_NN.md` exists for every week.** Honest record-keeping is a prerequisite for handling real money.

If gates pass: deploy $500 of real capital, re-evaluate after another 6 weeks before scaling. Re-allocation between sleeves continues at 4-week cadence throughout.

If gates don't pass: refactor and continue paper, OR accept the result as a research dashboard (still valuable). Both are wins.

---

## 14. Things I don't know (carried forward — to revisit)

Listed here so they get answered with data, not assumed:

- **Optimal Opus deep-dive context size.** Starting at ~150–200K tokens; will measure memo quality (Sonnet-rated against rules baseline) and adjust by week 3.
- ~~Whether `alpaca-py` supports Level-3 multi-leg options.~~ ✅ **Resolved (research file 05): YES.** Version 0.43.2 supports MLEG single-call orders via `OrderClass.MLEG` + `OptionLegRequest`. Hard cap 4 legs. Bracket/OTO not supported on MLEG (manage stops client-side). Paper has Level 3 enabled by default.
- ~~Whether Anthropic Batch API supports tool use.~~ ✅ **Resolved (research file 05): YES.** Full Messages API parameter shape supported, including `tools`, `tool_choice`, prompt caching (stacks with the 50% batch discount), and extended thinking. Workspace-level cache isolation since 2026-02-05 — keep batch + live agent in same workspace.
- ~~Whether `vectorbt` performance is acceptable on your Mac.~~ ✅ **Resolved (research file 06): VIABLE.** Proxy benchmark on Linux container shows full WF sweep (~24×100 combos) completes in ~4.5s; on M2/M3 with real vectorbt expect 3–10s wall-clock, RAM under 500MB. Bottleneck will be data ingestion + LLM steps, not vectorbt.
- ~~tmux vs. launchd preference.~~ ✅ **Resolved: tmux during build, launchd when stable.** Defaults written above.
- **Whether earnings call transcripts are reliably free-tier accessible.** They are NOT on Finnhub free tier. Two paths: (a) skip transcripts and use 10-Q narrative + earnings press releases via EDGAR (free, lower-fidelity), or (b) upgrade to Finnhub paid ($35/mo) to unlock transcripts (recommended v1.5). Decision deferred to after milestone 7.
- **Whether IRS guidance on crypto wash-sales has shifted in 2026.** Defaulting to "wash-sale rule does not apply to crypto" per current guidance, but bot enforces a 31-day re-entry block on crypto anyway as discipline.

---

## 17. Data sources — the free-tier stack (locked)

Per user direction: **no paid data sources**. Total monthly cost: $0. We work harder on the free stack to make it as good as it can be. Concretely:

| Source | What it gives us | Limits |
|---|---|---|
| **Alpaca IEX** (built-in, free) | Real-time equity quotes/bars, websocket streaming, paper trading sandbox | IEX-only feed (not full SIP) — fine for our daily/weekly cadence |
| **Alpaca crypto** (built-in, free) | 24/7 BTC/ETH/SOL pricing | Fees on actual trades (0.25%/side) but data is free |
| **SEC EDGAR** (always free, no key) | Authoritative 10-K, 10-Q, 8-K, proxy filings, insider trades (Forms 3/4/5), Schedule 13D/G institutional ownership | 10 req/sec rate limit, must set real User-Agent string. JSON endpoints are clean. |
| **Finnhub free tier** | Company news, basic sentiment, earnings calendar, basic fundamentals, recommendations, IPO calendar | ~60 calls/min. **No earnings call transcripts** (paywalled). |
| **Yahoo Finance** (via `yfinance` library) | Fundamentals, historical prices, options chains, analyst estimates, earnings dates | Unofficial scrape; rate-limit yourself; cache aggressively |
| **RSS feeds** | Yahoo Finance per-ticker, MarketWatch, Seeking Alpha public tags, Reuters, AP business, Bloomberg public RSS | Cheap, reliable, low-latency for headlines |
| **Reddit / X (sparingly)** | r/wallstreetbets sentiment, FinTwit color | Treat as "color," not signal. Rate-limited and noisy. |
| **NewsAPI free** | Broad headlines | 24h delay — only useful for sentiment context, not trading |
| **AlphaVantage free** | One-off lookups | 25 req/day in 2026; basically a backup |
| **CoinGecko free** | Crypto market cap, volume, sentiment | Decent, no key needed |
| **FRED (St. Louis Fed)** | Yields, CPI, unemployment, money supply, every macro series | Free API, real authoritative source. Underused by retail. |
| **Treasury.gov** | Bill/note auction results, federal debt | Free |

**What this stack can deliver to Opus deep-dives** (the highest-context use case):
- Full 10-Q and 10-K text (EDGAR — high signal)
- Last 4 quarters' earnings press releases via 8-K filings (EDGAR — close substitute for transcripts; the "what management said" comes from Q&A which IS missing without paid transcripts, but the prepared remarks and selected metrics are in the 8-K)
- Insider transactions over last 12 months (EDGAR Form 4)
- Institutional ownership changes (EDGAR 13F/13G)
- Analyst recommendations and price target changes (Finnhub free)
- 90 days of news headlines + sentiment (Finnhub + RSS)
- Macro context (FRED)
- Sector ETF performance (Alpaca)

**The honest gap from skipping paid sources:** Opus deep-dives lose the *Q&A portion* of earnings calls — where analysts pin management down on hard questions and where new information sometimes leaks. Mitigation: many financial journalists summarize Q&A within 24h via free RSS (Seeking Alpha, MarketWatch, Bloomberg). We instruct Opus's deep-dive prompt to prioritize the post-earnings press coverage as a transcript proxy.

**Verdict:** the free stack is sufficient to know whether the architecture works. If after 6 weeks the system is consistently outperforming and you want to push further, paid Finnhub becomes a sensible $35/mo upgrade — but that's a v2 decision driven by data, not now.

## 16. Leverage system (full spec — derived from research file 07)

The user's "free-thinking traders" mandate plus a `MASTER_CAPABILITY` slider raises the question of *how* leverage should work. The institutional answer, validated across CTAs (AQR, Man AHL), risk parity (Bridgewater), prop desks, and the LLM-trading-agent academic literature (TradingAgents arXiv 2412.20138, TradeTrap arXiv 2512.02261): **boring mechanical Python at the cap layer; expressive LLM at the decision layer.**

### 17.1 Per-agent base parameters (at MASTER_CAPABILITY = 1.0)

| Agent | base_max_gross | base_vol_target | Reasoning |
|---|---:|---:|---|
| Haiku (trend) | **1.50×** | 14% | Trend Sharpe scales linearly with leverage; signal is broad and diversified |
| Sonnet (multi-factor) | **1.25×** | 12% | Quality-tilted, less convex; some concentration; factor leverage has diminishing returns |
| Opus (concentrated GARP) | **1.00×** | 11% | Concentration risk doesn't diversify with leverage; idiosyncratic gap risk is non-linear |
| Manager | n/a | n/a | Doesn't take direct positions; sets `MASTER_CAPABILITY` and reallocates capital |

At `MASTER_CAPABILITY = 0.5` → caps become 0.75× / 0.625× / 0.5× (effectively long-only with cash buffer).
At `MASTER_CAPABILITY = 1.5` → caps become 2.25× / 1.875× / 1.5× (Haiku → Reg-T 2× ceiling, Python clips).

### 17.2 Vol-targeting math

```
realized_vol_t = EWMA(daily_returns, λ=0.94)        # ~20-day half-life, RiskMetrics standard
realized_vol_t = max(realized_vol_t, 0.08)           # floor — prevents vol-paradox lever-up
sizing_multiplier = min(target_vol / realized_vol_t, 1.75)  # cap on implied multiplier
```

- **Recompute nightly**, applied at next morning open.
- **Day-over-day change in target leverage capped at ±10%** to avoid whipsaw on a single noisy day.
- The 8% realized-vol floor is the math-layer fix to the volatility paradox: a calm 5% realized vol would otherwise push leverage to 14%/5% = 2.8×.

### 17.3 Drawdown-leverage ladder (per sub-portfolio against its own 30-day high)

| Drawdown | Scalar | Note |
|---|---:|---|
| < 5% | 1.00× | Normal |
| 5–10% | 0.75× | Yellow |
| 10–15% | 0.50× | Orange |
| 15–25% | 0.25× | Red |
| > 25% | 0.00× | Forced cash; Manager review required to re-enable |

**Recovery rule:** re-enter prior bucket only after the portfolio sits inside the better bucket for **5 consecutive trading days**. Prevents whipsaw re-leveraging into a dead-cat bounce.

This ladder sits *inside* each sub-portfolio. The aggregate-portfolio drawdown ladder from §5 Layer 3 (-15% halve / -25% pause / -33% liquidate) sits *on top* — the more conservative trigger always wins.

### 17.4 VIX-regime ladder (the volatility paradox)

| VIX (close) | Scalar | Rationale |
|---|---:|---|
| < 12 | 0.6× | Vol paradox — calm precedes shocks |
| 12–18 | 1.0× | Sweet spot |
| 18–25 | 0.8× | Trim |
| 25–35 | 0.5× | Stress |
| > 35 | 0.25× | Crisis |

Multiplicative on `effective_max_gross` after the drawdown scalar. Both fire automatically; LLM cannot override.

### 17.5 Leveraged ETF policy

**Allowed for tactical short-term holds (≤5 trading days, Python auto-liquidates on day 6).** Banned for strategic positions.

- Whitelist: TQQQ, SQQQ, UPRO, SPXU, SOXL, SOXS, TMF, TMV.
- Blacklist: single-stock 2x/3x ETFs (TSLL, NVDL, AMZD, etc.) — too much idiosyncratic gap risk.
- Python tracks `entry_date` of every LETF position and force-closes at next open on day 6.
- Agent prompts mention this rule explicitly so the LLM doesn't plan around it.
- **Anti-rotation rule:** Python flags >2 reopens of the same effective exposure (e.g., TQQQ → UPRO → TQQQ) within 15 trading days for Manager review — catches LLM rule-rotation gaming.

### 17.6 Options policy

**Allowed:** defined-risk multi-leg structures only.
- Long debit verticals (call/put spreads).
- Short credit verticals (both legs defined).
- Iron condors / iron butterflies.
- Covered calls (against existing equity).
- Cash-secured puts (with cash held separately).

**Banned:** naked anything, including naked long calls/puts (yes, even though risk is technically defined — agents will repeatedly lose 100% of premium and rationalize it as "small position"). Synthetic stock, calendar/diagonal spreads (Greek complexity exceeds reliable LLM reasoning).

Per-agent options budget: **max 20% of sub-portfolio in defined-risk options at any time**. Options exposure counts toward gross leverage at notional delta exposure, not premium paid.

### 17.7 Manager's role in leverage

The Manager owns `MASTER_CAPABILITY`. Default 1.0. Mandatory adjustments:
- Cut to 0.75 when any sub-portfolio enters the 5–10% drawdown bucket.
- Cut to 0.5 when any sub-portfolio enters 10%+ drawdown.
- Raise toward 1.25 only after the system has run ≥6 weeks with realized aggregate Sharpe > 0.8 and aggregate max DD < 7%.
- Never above 1.5 without explicit `OVERRIDE_KEY`.
- Reallocates capital between sleeves on rolling 30-day **Sharpe** (not absolute return) — leverage already amplifies absolute returns, so Sharpe is the honest comparison.

### 17.8 Dashboard tiles for leverage

- Current `MASTER_CAPABILITY` value + timestamp of last change + slider control.
- Per-agent: `effective_max_gross`, current realized gross, current realized 20-day vol, current 30-day max DD, current dd-bucket, current VIX-bucket scalar.
- Portfolio-level: realized vs. target vol (rolling), gross + net leverage, cash %, % in LETFs, % in options.
- "Leverage Budget Used" gauge per agent: `realized_gross / effective_max_gross`.
- **Friction ledger**: cumulative slippage + commissions + simulated borrow cost as % of NAV (the "is leverage paying for itself?" honesty metric).

### 17.9 Manager weekly journal additions

- "Leverage events" log: every regime/dd-bucket change, every cap-breach attempt Python rejected, every LETF auto-liquidation.
- One paragraph: "did leverage help or hurt this week?" — decompose return into beta, alpha, and leverage-amplification.
- Top three leverage decisions of the week with retrospective grade.
- If frictions > 50bps/month, Manager mandated to cut MASTER_CAPABILITY by 25% the following week.

### 17.10 Honest pre-mortem (top 7 leverage failure modes + the catch)

1. **Position infatuation:** an agent (probably Opus) tops up a single losing name to 30%+ of its sleeve. **Catch:** per-position cap (15% Sonnet, 18% Opus, 25% Haiku), aggregated drawdown ladder, VIX gate.
2. **Volmageddon-style quiet-then-spike:** vol-target math pushes leverage to cap during a calm stretch; then vol regime flips. **Catch:** vol paradox haircut at low VIX, 8% realized-vol floor, 1.75× implied-multiplier cap.
3. **Rule-rotation gaming:** LLM rotates TQQQ → UPRO → TQQQ to defeat the 5-day LETF cap. **Catch:** anti-rotation rule (>2 reopens / 15 days flagged); friction ledger surfaces the cost.
4. **Ladder fires at the bottom:** drawdown ladder forces cash at the local low, recovery rule keeps leverage low while market V-shapes back. **Catch:** accept it. It's the cost of insurance vs. the alternative (1-in-N year blow-ups). Manager journal documents so it's not relitigated.
5. **Adversarial input:** hallucinated/spoofed news drives one agent to brush the cap on a single LETF. **Catch:** per-order Python cap check (cannot exceed cap *after* order), per-day order count cap (≤8/agent), Manager veto on any single order >5% of sub-portfolio.
6. **Friction overwhelms alpha:** slippage + commissions on a $3K account at 2× turnover destroys the "beat SPY net of costs" mandate. **Catch:** friction ledger as Manager weekly KPI; auto-MC-cut if frictions > 50bps/month.
7. **PDT trips on a leveraged intraday:** day-trade limit blocks legitimate intraday close, leaves a leveraged position open overnight unintentionally. **Catch:** day-trade counter visible to all agents in their context; Python pre-trade rejection of orders that would force a 4th day-trade in the 5-day window.

### 17.11 Six-week leverage observability gate

Before any consideration of raising `MASTER_CAPABILITY` above 1.0:
1. Six full weeks of paper operation under MC = 1.0.
2. Aggregate Sharpe ≥ 0.8 over that window.
3. Aggregate max DD ≤ 7% over that window.
4. Friction ledger ≤ 50 bps/month.
5. No `RECONCILIATION_BREAK` events; no manual kill-switch trips other than intentional drawdown ladder firings.

If gates pass, Manager may propose MC = 1.10 → 1.25 in 0.05 increments, one increment per week, gated on continued performance. Never auto-raised by Manager — proposed for human approval via the dashboard.

---

## 18. Reference: agent prompts

Drafts of all four agent prompts live in `prompts/`:

- `prompts/haiku_agent.md` — trend follower, equity + crypto dual mandate
- `prompts/sonnet_agent.md` — multi-factor quant
- `prompts/opus_agent.md` — concentrated discretionary, includes both daily and deep-dive variants
- `prompts/manager_agent.md` — CIO with six call types (regime read, critique, reallocation, risk check, drawdown response, weekly journal)

All four are designed to be cached as 1h-TTL system prompt prefixes, with per-call user messages providing `{{double-brace}}` variables. Strict JSON outputs (manager weekly journal is markdown). Hard rules in each prompt are advisory only — Python's RiskGate enforces actual constraints.

These prompts will iterate based on calibration data once the bot is live. v1 of the prompts is **deliberately minimal** to keep cached-prefix cost low and to make A/B testing tractable.

---

*End of blueprint v0.3. See `01_HONEST_ASSESSMENT.md` for the candid take, `CHANGELOG.md` for what changed since v0.1.*
