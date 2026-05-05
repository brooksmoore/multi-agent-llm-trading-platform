# Autotrader Frameworks & Architecture Research

**Date:** 2026-04-24
**Project:** Multi-Agent Asset Competitive Bot
**Scope:** Production-grade Python autotrader for 4 Claude LLM agents (Haiku/Sonnet/Opus + Manager) executing on Alpaca paper, with a Bloomberg-terminal-style dashboard at localhost:8081.

---

## 1. Backtesting Frameworks (state of the art, April 2026)

| Framework | Strengths | Weaknesses | Learning Curve | Fit for this project |
|---|---|---|---|---|
| **vectorbt (OSS) / vectorbt PRO** | Vectorized over NumPy/Numba; minutes to test millions of parameter combos; portfolio simulation, Plotly viz built-in. PRO adds tick-level, multi-asset portfolios, CV pipelines. | Mental model is non-trivial — everything is broadcast arrays. PRO is paid (~$400/yr). Less natural for event-driven, agent-driven flows. | Medium-high | Excellent for the *parameter sweep / strategy benchmarking* harness, weak for the *agent decision* simulation. |
| **backtrader** | Mature, event-driven, lots of community recipes, supports live IB/Oanda. Easy to map a `Strategy` class onto an agent. | Project is essentially unmaintained since 2023 (community forks only). Slow vs. vectorized engines. Plotting is dated. | Low-medium | Good ergonomic match for agent-style "on each bar, decide" loops, but stagnation is a real risk. |
| **zipline-reloaded** | Quantopian heritage, pipeline API for cross-sectional factor research, integrates well with `pyfolio-reloaded` / `alphalens-reloaded`. | Heavy bundle/data-ingest ceremony. Daily-bar bias. Less active than vectorbt. | Medium | Useful only if you go deep into factor research; overkill here. |
| **QuantConnect LEAN** | Production-grade, C# core with Python bindings, has live-trading adapters for Alpaca/IBKR/etc. Same code backtests and goes live. | Heavyweight: Docker, data subscriptions, opinionated project layout. Locks you into LEAN idioms. | High | Worth it if you want unified backtest/live, but a lot of machinery for a 3000-line project. |
| **bt** | Simple, readable; great for portfolio-level rebalancing strategies (target weights, monthly rebal). | Not designed for tick/intraday or order-book sims. | Low | Useful as a **secondary** sanity-check engine for the manager-agent's allocation decisions. |
| **nautilus_trader** | High-performance Rust core with Python API, true event-driven, microsecond bus, built-in OMS / risk engine, supports multi-venue. The most "production-OMS-shaped" of the OSS options as of 2026. | Heaviest learning curve; opinionated actor/messaging model; documentation still maturing. | High | The closest OSS engine to a real shop; if you want one engine that goes from backtest -> paper -> live with the same code, this is the strongest 2026 pick. |

**Verdict for the comparison harness:** use **vectorbt** for fast parameter/strategy sweeps and **bt** for monthly portfolio rebalancing sanity checks. For the unified backtest+live engine, **nautilus_trader** is the cleanest 2026 choice; **backtrader** is the pragmatic shortcut if you want to ship in a weekend.

---

## 2. Live Execution Architecture

### Event-driven vs. polling
- **Polling** (REST every N seconds) is fine for daily/hourly strategies and is what most agent-based bots ship first. Simple, debuggable, no socket reconnection logic.
- **Event-driven** (websocket bars + trade/quote streams) is mandatory the moment you care about intraday fills, stops, or news-reactive trades. Alpaca's SDK exposes `StockDataStream` and `TradingStream` — the latter pushes order/fill updates so your OMS doesn't have to poll order status.
- For a multi-agent LLM bot the right hybrid is: **websocket for prices/fills** (fast lane) + **REST poll every 60s for reconciliation** (slow lane). Never trust just one.

### OMS pattern
A clean OMS sits between Strategy and Broker and owns the *only* mutable view of positions:

```
Strategy/Agent --(Order intent)--> RiskGate --(approved)--> OMS --(broker call)--> Broker
                                                              ^                       |
                                                              +----(fill events)------+
```

The OMS responsibilities:
1. Assign a deterministic `client_order_id` (UUIDv7 or `agent:strategy:bar_ts`).
2. Persist the order intent **before** firing it to the broker.
3. Subscribe to the trading stream and update local state on `new`, `partial_fill`, `fill`, `canceled`, `rejected`.
4. Emit OMS events on a pub/sub bus for the dashboard / loggers.

### Trade lifecycle state machine
```
PROPOSED -> APPROVED -> SUBMITTED -> ACK -> WORKING -> {PARTIAL -> WORKING} -> FILLED -> RECONCILED
                  |          |        |        |                                 |
                  +-> REJECTED       +-> ERROR +-> CANCELED                      +-> SETTLED
```
Implement this as a tiny `enum.Enum` + a transition table (`{from_state: {event: to_state}}`). Reject illegal transitions loudly. Persist every transition with a monotonic `seq` number for audit.

### Separating strategy from execution
The standard pattern is **Order Intents** (a.k.a. "signals" or "target portfolio"):

- The Strategy / Agent emits *desired state*, not API calls. E.g. `TargetPosition(symbol="AAPL", weight=0.05, reason="Sonnet thesis #42")`.
- A **Trader / ExecutionPlanner** translates desired state vs. current positions into actual orders, applying execution policy (TWAP slice, marketable limit, etc.).
- This means: agents are pure functions of (market state, memory) -> intents. They are testable without a broker and swappable without touching execution code.

### Idempotency & crash recovery
- **Client order IDs** must be deterministic so a restart that retries the same intent doesn't double-submit. Alpaca enforces uniqueness on `client_order_id`, so a duplicate POST is rejected — that's your safety net.
- **WAL-style intent log**: append every Intent + every state transition to an append-only file (or SQLite WAL table) *before* the broker call. On boot, replay the log: any intent without a terminal state gets reconciled against the broker's open orders.
- **Reconciliation loop** on startup: pull broker positions, broker open orders, last-N broker activities; diff against local state; emit `RECONCILED` or open an alert.
- Never trust local state alone — the broker is the source of truth. Local state is a cache for speed and an audit log for explainability.

---

## 3. Risk Management Patterns

### Pre-trade risk gate
A single chokepoint that every intent passes through. Each check is a small pure function returning `(ok, reason)`:
- max position size (% of equity)
- max single-name concentration
- max gross / net exposure
- max orders per minute per agent (rate limit)
- restricted-symbol blocklist (e.g. leveraged ETFs, OTC)
- min price / min ADV (avoid penny-stock / illiquid)
- buying-power check (broker reports it; double-check locally)

### Position sizing
Standard production menu, in increasing sophistication:
1. **Fixed-fractional** ($X or X% per trade)
2. **Volatility-targeted** (size = target_vol / realized_vol * equity)
3. **Kelly / fractional-Kelly** (only if you have a real edge estimate — agents don't)
4. **Risk parity** across the agent sleeves (the manager agent's natural job)

For an LLM-driven bot, **vol-targeted** with a hard cap and a per-agent equity sleeve is the right default. Don't let an agent that "feels strongly" override the sizing module.

### Kill switches & circuit breakers
- **Daily loss limit** (e.g. -2% intraday): flip a global `TRADING_HALTED` flag, cancel all open orders, refuse new intents until manual reset.
- **Drawdown limit** (e.g. -10% from equity high-water): halt + alert.
- **Per-agent kill switch** (e.g. an agent that loses N trades in a row gets benched).
- **Broker-side bracket / stop-loss** as a backstop — if the bot dies, the broker still protects you.
- **Max orders per minute** circuit breaker — protects against an LLM that decides to rebalance 200 times.
- **Heartbeat** — if the main loop hasn't ticked in 60s, halt.

### Local vs. broker reconciliation
Run a `Reconciler` task every minute:
- Pull broker positions, compare to local positions table.
- If mismatch > tolerance (1 share or $1), log a `RECONCILIATION_BREAK` event and (in strict mode) halt trading.
- Reconcile cash balance, open orders, day-trade count.

---

## 4. Data Pipeline Patterns

### Market data ingestion
- **Websocket** for live bars/trades/quotes. Alpaca's `StockDataStream` gives 1-min bars on the free IEX feed, full SIP on paid.
- **REST** for historical backfill and gap fills after reconnect.
- Wrap both behind a `MarketData` interface so backtests use a CSV/Parquet replay and live uses the websocket — same code path.

### OHLCV storage
The 2026 consensus for a single-machine quant project:
- **DuckDB** for query-time analytics over Parquet files. Zero-config, columnar, joins beautifully with pandas/polars, handles tens of GB on a laptop. This is the right answer for this project.
- **Parquet** files partitioned by `symbol/year=YYYY/month=MM` on disk.
- **SQLite** for transactional state (orders, positions, intents, agent memos) — small, ACID, single file, perfect for the OMS log.
- Skip Postgres/TimescaleDB unless you outgrow a single machine.

### News / sentiment (free tier, April 2026)
- **Finnhub free tier** — company news, basic sentiment, earnings calendar. ~60 calls/min.
- **NewsAPI** — broad headlines but 24h delay on the free plan; useful for sentiment but not for trading.
- **SEC EDGAR** — free, no key, authoritative for 10-K/10-Q/8-K. Use the JSON endpoints; respect the 10 req/s limit and set a real User-Agent.
- **RSS** — Yahoo Finance, Seeking Alpha, MarketWatch tickers; cheap and reliable.
- **Reddit / X** — useful but rate-limited and noisy; treat as "color," not signal.
- **AlphaVantage free** — 25 req/day in 2026, basically only useful for one-off lookups.

Feed everything into a normalized `NewsItem(ts, symbols, source, url, title, body, sentiment?)` table. This is what the LLM agents read.

### Caching
- **Disk cache** with `diskcache` or a thin DuckDB wrapper, keyed by `(endpoint, params, date)`. TTL by data type (price = 1 min, fundamentals = 1 day, filings = forever).
- **In-memory LRU** for hot paths in the agent loop.
- Always cache the **raw** API response, not the parsed object — schemas change.

---

## 5. Common Failure Modes

The graveyard of retail bots is large. The main causes:

1. **Look-ahead bias**: using a bar's close to make a decision *during* that bar, or using a fundamentals figure before it was actually published. Fix: every data point gets a `knowledge_ts` (when you could actually have known it) separate from the `as_of_ts`.
2. **Survivorship bias**: backtesting only currently-listed tickers. Your S&P backtest looks great because you implicitly removed the bankruptcies. Fix: use point-in-time index constituents, or accept the bias and discount returns by 1-2%/yr.
3. **Overfitting** from parameter sweeps — vectorbt makes it terrifyingly easy to test 100k combos and pick the winner. Fix: walk-forward CV, holdout periods, deflated Sharpe (Bailey & López de Prado), and a hard rule that the live strategy has ≤3 tunable parameters.
4. **Slippage / spread underestimation**: backtests assume mid-fill, live gets the spread. Fix: model fills at far-touch + 1bp + impact, and verify against actual paper-trading fills weekly.
5. **Latency assumptions**: "I'll see the bar close at 09:30:00 and submit by 09:30:00.05" — in practice you see it 200-800ms late and your order arrives after a hundred others. Fix: design strategies that work on the *next* bar, not the close-of-bar.
6. **Lack of monitoring**: bot dies at 02:00, you find out at the open. Fix: heartbeat, structured logs, push alerts (Pushover, Telegram, ntfy.sh) on any state transition to HALTED, and a daily 08:00 P&L email.
7. **API quota exhaustion** mid-day (especially LLM tokens). Fix: per-agent token budget, fallback to cheaper model, and a "respond from memory" path.
8. **Regime change**: a strategy that ran for 6 months breaks when vol regime flips. Fix: monitor live vs. backtest performance gap; auto-bench a strategy that drifts beyond N-sigma.

---

## 6. Dashboard / Terminal Patterns

For a Bloomberg-terminal-lite at `localhost:8081`:

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Plotly Dash** | Charts are first-class; reactive callbacks; easy multi-panel grid; many fintech examples. | Callback model gets gnarly past ~10 components; styling fights you to look "terminal." | Best balance for this project. |
| **Streamlit** | Fastest to prototype; great for analyst tools. | Re-runs the whole script on every interaction; not designed for real-time multi-panel; hard to make look like a terminal. | Use for ad-hoc agent reviews, not the main UI. |
| **FastAPI + HTMX + Jinja** | Server-driven, tiny JS surface, SSE/websockets for live updates, looks however you style it. Aligns naturally with the FastAPI you'll already have for the bot's REST surface. | You write the charts (or embed Plotly via JSON). More code. | Best if you want it to *actually* look like a terminal and are comfortable with HTML/CSS. |
| **Flask + vanilla JS / Lightweight Charts** | Maximum control, smallest deps. TradingView's Lightweight Charts is free and looks pro. | All wiring is on you. | Overkill for a single-user local dashboard. |

**Real-time vs. polled:** for a single-user local dashboard, **2-5s polling** is fine and a tenth of the engineering of websockets. Reserve websockets for the price/fill ingestion path inside the bot, not the UI.

**Recommended layout** (4-pane grid, dark theme, monospace):
- **Top bar**: account equity, day P&L, buying power, halted/live status, time since last heartbeat.
- **Top-left**: per-agent equity curves (one line per agent + benchmark SPY). Toggle between absolute and relative-to-benchmark.
- **Top-right**: current positions table (symbol, agent, qty, avg cost, mark, P&L, % of NAV).
- **Bottom-left**: agent memos feed — the latest LLM rationale per agent, with timestamp and the trade it justified. This is the killer feature for a fund-manager review.
- **Bottom-right**: trade log (last 50 fills) + intent log (last 50 proposals incl. rejected ones with reason).
- **Modal / drawer**: click any trade -> full agent prompt/response, market context at decision time, fill quality.

---

## 7. Recommended Reference Architecture

### Stack (concrete picks for THIS project)

- **Language**: Python 3.12, `uv` for env mgmt, `ruff` + `mypy --strict` on agent and execution modules.
- **Broker**: `alpaca-py` (official SDK, supports both REST and the trading/data streams).
- **LLM**: `anthropic` SDK, async client, with a thin wrapper that enforces per-agent token budgets, retries on overload, and logs every prompt/response to disk.
- **Backtest**: `vectorbt` (free) for parameter sweeps; a small custom event loop that *reuses the live agent code* for the realistic agent-driven sim. Don't try to run the LLM agents inside vectorbt — too slow and not its model.
- **Data store**: DuckDB + Parquet for market/news; SQLite for OMS state and agent memory.
- **Cache**: `diskcache` for HTTP responses, plus an LRU on the hot agent path.
- **Web/API**: FastAPI (you already need it for the dashboard backend and webhooks).
- **Dashboard**: **Plotly Dash** with a custom dark "terminal" CSS, polling every 3s. (If you want it to *really* look like Bloomberg, swap to FastAPI + HTMX + TradingView Lightweight Charts later — keep the data layer the same.)
- **Scheduler**: `APScheduler` for the daily reconciliation, end-of-day reports, and morning prep.
- **Messaging / events**: an in-process pub/sub (`blinker` or a 50-line custom `EventBus`) — no Kafka, no Redis. Single process, single machine.
- **Persistence of agent memory**: SQLite table `agent_memory(agent_id, ts, kind, content_json)` plus a Markdown daily journal file per agent for human readability.
- **Alerting**: `ntfy.sh` (free, no account) for HALTED / RECONCILIATION_BREAK / heartbeat-missed.
- **Tests**: `pytest`, with a `FakeBroker` that implements the same interface as `AlpacaBroker` for unit tests of the OMS state machine.

### Folder layout (~3000 LOC target)

```
multi_agent_bot/
  pyproject.toml
  .env.example                 # ALPACA_KEY, ANTHROPIC_KEY, NTFY_TOPIC, etc.
  README.md
  app.py                       # entrypoint: starts bot loop + dashboard

  config/
    settings.py                # pydantic-settings, all knobs in one place
    agents.yaml                # per-agent model, sleeve %, risk caps, prompt path
    universe.yaml              # tradable symbols, blocklist

  core/
    events.py                  # EventBus, Event dataclasses
    state_machine.py           # generic FSM helper
    clock.py                   # wall clock vs. backtest clock abstraction
    types.py                   # Intent, Order, Fill, Position, NewsItem, AgentMemo

  data/
    market.py                  # MarketData interface; AlpacaMarketData, ReplayMarketData
    news.py                    # Finnhub/EDGAR/RSS adapters -> NewsItem
    store.py                   # DuckDB + Parquet read/write
    cache.py                   # diskcache wrapper

  agents/
    base.py                    # Agent ABC: .observe() -> list[Intent]
    haiku_agent.py             # fast, high-frequency screener
    sonnet_agent.py            # balanced, swing trader
    opus_agent.py              # deep-research, low-frequency
    manager_agent.py           # allocates capital across the three; can override
    prompts/                   # one .md per agent, version-controlled
    memory.py                  # SQLite-backed memory + daily Markdown journal
    llm.py                     # anthropic client wrapper, budget, retries, logging

  execution/
    risk.py                    # pre-trade RiskGate, all checks
    sizing.py                  # vol-targeted sizing, sleeve caps
    oms.py                     # OMS, owns the trade-lifecycle FSM
    broker.py                  # Broker ABC; AlpacaBroker, FakeBroker
    reconciler.py              # 60s reconciliation loop
    kill_switch.py             # global + per-agent halts, daily loss limits

  backtest/
    engine.py                  # event-driven sim using the same OMS + agents
    sweep.py                   # vectorbt parameter sweeps for non-LLM strategies
    metrics.py                 # Sharpe, Sortino, max DD, deflated Sharpe

  dashboard/
    server.py                  # Dash app, polls SQLite/DuckDB every 3s
    layout.py                  # the 4-pane grid + top bar
    components/                # equity_chart.py, positions_table.py, memos_feed.py, trade_log.py
    theme.css                  # the "terminal" look

  ops/
    alerts.py                  # ntfy.sh push
    heartbeat.py               # writes a tick every loop; watchdog reads it
    journal.py                 # daily Markdown report generator
    schedules.py               # APScheduler jobs

  tests/
    test_state_machine.py
    test_risk_gate.py
    test_oms_recovery.py       # crash mid-trade, restart, reconcile
    test_agents_offline.py     # agents against canned market data
    fixtures/
```

### Key design patterns to lean on
- **Hexagonal / ports-and-adapters**: `Broker`, `MarketData`, `LLM`, `News` are all interfaces. Adapters for Alpaca/Anthropic/Finnhub live at the edges. The core (agents, OMS, risk) depends only on the interfaces. This is what makes backtest == live possible.
- **Intents, not actions**: agents emit declarative intents; the OMS decides how/whether/when to execute.
- **Event sourcing for the OMS**: every state change is an append-only event; current state is a fold over events. Crash recovery is just "replay the events."
- **Single source of truth = the broker**: local state is a derived cache, reconciled every minute.
- **Tight feedback loop**: every LLM call's prompt + response + resulting intent + eventual fill is linkable by `intent_id` so you can do post-mortems and prompt iteration.

### Build order (suggested)
1. Types, EventBus, FakeBroker, FSM, OMS — get to a working order lifecycle with no real broker, no LLM.
2. Risk gate + sizing + kill switch with unit tests.
3. AlpacaBroker adapter, paper credentials, end-to-end "submit a hardcoded order" smoke test.
4. MarketData (Alpaca websocket + DuckDB store), 1 week of bars cached.
5. Agent base, one Haiku agent emitting random intents, end-to-end paper trade.
6. Real prompts for all 4 agents; manager agent with sleeve allocation.
7. Reconciler, alerts, heartbeat.
8. Dashboard last — it's a read-only view of SQLite/DuckDB and shouldn't be coupled to the bot loop.

This keeps every milestone runnable and demoable, and the dashboard never blocks live trading from being built.

---

### TL;DR Recommendation

Use **alpaca-py + anthropic + DuckDB + SQLite + FastAPI + Plotly Dash**, with a **hexagonal architecture** that puts agents and OMS in the core and pushes Alpaca/Anthropic/Finnhub to adapters. Use **vectorbt** for offline parameter sweeps and a **custom event-driven sim** (sharing code with the live OMS) for agent backtests. Persist every order intent and state transition to an append-only SQLite log so a crash mid-trade is a non-event. Put a **single pre-trade risk gate** and a **global kill switch** in front of every order, and reconcile against Alpaca every 60 seconds. The dashboard polls; the bot streams. Total code budget: ~3000 lines is realistic if you stay disciplined about keeping LLM logic out of the OMS and execution logic out of the agents.
