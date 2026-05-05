# 04 — Alpaca API & $1/Day Anthropic Budget

Research date: 2026-04-24
Sources: docs.claude.com, platform.claude.com, alpaca.markets, docs.alpaca.markets, github.com/alpacahq/alpaca-py, status.alpaca.markets, plus secondary aggregators cross-checked against primary docs.

> Note on egress: `docs.alpaca.markets` is not on the cowork web-fetch allowlist for this session, so Alpaca-specific facts below were assembled from WebSearch summaries of the official docs (and Alpaca's blog / GitHub / status page) rather than direct page fetches. Re-verify any number marked **[VERIFY]** in the Alpaca dashboard before going live.

---

## PART A — ALPACA API (April 2026)

### 1. Paper-trading sandbox

| Item | Detail |
|---|---|
| Cost | Free, included with any Alpaca account |
| Default starting equity | $100,000 USD |
| Funding limits | Arbitrary — set whatever balance you want at reset (1¢ to billions) |
| Reset | Manual via dashboard (or `DELETE /v2/account/configurations` workflow); wipes history and **invalidates the existing API key** — must regenerate |
| Market data | Real-time IEX feed for equities, indicative feed for options, real crypto pricing — all free on the Basic plan |
| Realism | Simulates PDT checks; orders route to a simulator, not a live exchange — fills are heuristic, not guaranteed to match real microstructure |

Gotcha: a paper account flagged for PDT can get stuck at $0 buying power until you reset (see forum thread "Paper Trading Flagged for PDT - Now New Accounts stay at $0").

### 2. Asset classes

| Asset | Supported | Fractional | Notes |
|---|---|---|---|
| US Equities | Yes | Yes — ~2,000+ symbols, $1 minimum | IEX real-time free; SIP requires paid Algo Trader Plus |
| Options | Yes | No (1 contract = 100 shares) | Commission-free; Level 1/2/3 available |
| Crypto | Yes | Yes | 24/7; 56 pairs, 20+ assets, USD/USDT/USDC/BTC quotes |
| Futures | No | — | Not offered — use a different broker if you need /ES, /CL, etc. |
| Forex | No | — | Not offered |

2026 changes: Alpaca expanded multi-leg (Level 3) options to all paper accounts by default, and continues to publish blog posts about API growth. No major asset-class additions since 2025; the platform remains stocks + options + crypto only.

### 3. Python SDK — `alpaca-py`

- **Yes, `alpaca-py` is the official SDK.** The legacy `alpaca-trade-api-python` is deprecated; new code should use `alpaca-py`.
- Repo: `github.com/alpacahq/alpaca-py`, Apache-2.0, Python 3.8+.
- Install: `pip install alpaca-py`
- Search did not pin the exact PyPI version as of April 2026; treat the latest tag on the GitHub releases page as authoritative. **[VERIFY]** with `pip index versions alpaca-py`.
- Key client classes:
  - `TradingClient` — orders, positions, account
  - `StockHistoricalDataClient`, `CryptoHistoricalDataClient`, `OptionHistoricalDataClient`
  - `StockDataStream`, `CryptoDataStream`, `OptionDataStream` (websockets)
  - `TradingStream` — order/account update events

### 4. Rate limits & data feeds

| Resource | Limit |
|---|---|
| Trading REST API (Basic) | 200 requests / minute / account; 429 on overage |
| Market Data REST (Basic) | Tied to data subscription; Basic = IEX only |
| Websocket — equities (Basic) | 1 concurrent connection, IEX feed, max 30 channel subscriptions (trades+quotes combined) |
| Websocket — crypto | More generous, included free |
| SIP (full consolidated tape) | Paid subscription only; historical SIP usable if `end` is ≥15 min old |

Practical implication: a 4-agent system polling REST every few seconds for ~10 symbols is well under 200 RPM. Use websockets for live bars/trades — REST polling for quotes wastes the budget fast.

### 5. Cost basis / tax-lot accounting

Alpaca's `/v2/positions` returns aggregate `qty`, `avg_entry_price`, and unrealized P&L — **not per-lot data**. For tax-lot tracking (FIFO/LIFO/specific ID):

- Pull `/v2/account/activities?activity_types=FILL` to get every execution with timestamp, price, qty.
- Build your own lot ledger in SQLite/Parquet.
- Alpaca also provides annual 1099-B forms for live accounts that handle the IRS-side lot accounting, but the API itself does not expose lot-level basis.

For a multi-agent competitive bot you almost certainly want your own ledger anyway — agents need to know "which lot did Agent-Sonnet-4.6 actually open" to score performance.

### 6. PDT rule on sub-$25K accounts

- Same rule on paper as on live: 4+ day-trades in 5 business days = PDT flag; if previous-day equity < $25,000 the rejecting order returns HTTP **403**.
- Alpaca's "PDT protection" pre-checks and rejects the order that *would* trip the 4th day-trade — your code needs to handle 403 gracefully or check `pattern_day_trader` and `daytrade_count` on `/v2/account` before submitting.
- Workarounds: trade crypto (no PDT), trade options without same-day round-trips, or hold positions overnight.
- Recommendation for $1/day project: start paper at $30,000 to stay above the $25K threshold and keep day-trading buying power flowing, OR design strategies that hold ≥1 session.

### 7. Crypto specifics

| Item | Detail |
|---|---|
| Pairs | 56 pairs, 20+ assets (BTC, ETH, SOL, AVAX, DOGE, LTC, LINK, AAVE, etc.) |
| Quote currencies | USD, USDT, USDC, BTC |
| Fees | **0.25% of trade value** (volume-tiered, this is the headline retail rate) |
| Spread | Wider than Coinbase Advanced or Kraken Pro — Alpaca aggregates liquidity from market makers; expect 5–30 bps on majors, much wider on small-caps |
| Margin / shorting | Not allowed |
| Hours | 24/7 |
| Fractional | Yes, decimal precision varies by asset |

Honest take: Alpaca crypto is convenient because it lives in the same API as your equity book, but the 25 bps fee plus spread makes it noticeably more expensive than a dedicated exchange. For paper testing this is fine; for live deployment of HFT-style crypto strategies it will eat alpha.

### 8. Options approval

- **Paper accounts auto-receive Level 3** (multi-leg). Nothing to apply for.
- Live accounts must apply per level: Level 1 (covered calls / cash-secured puts), Level 2 (long calls/puts), Level 3 (spreads, straddles, iron condors, etc.).
- Approval is a real KYC/suitability process for live — paper sidesteps it entirely, which is great for a competitive multi-agent test bench.

### 9. Order types

| Type | Supported |
|---|---|
| Market | Yes |
| Limit | Yes |
| Stop | Yes |
| Stop-limit | Yes |
| Trailing stop | Yes |
| Bracket (`order_class=bracket`) | Yes — entry + take-profit + stop-loss in one POST |
| OCO (`order_class=oco`) | Yes — but only as exit legs after a position exists (not for opening) |
| OTO (`order_class=oto`) | Yes |
| Time in force | `day`, `gtc`, `opg`, `cls`, `ioc`, `fok` |
| Extended hours | Yes (`extended_hours=true` on limit orders) |

### 10. Gotchas / reliability

- StatusGator tracked **161+ outages over the past year** across Alpaca's surface. The data-streaming websocket (`wss://stream.data.alpaca.markets/v2/sip`) is the most-affected component (~159 incidents).
- Scheduled monthly maintenance: **2nd Saturday, 9–11:30 AM ET**. Don't run live trades during this window.
- Last published incident: April 11, 2026.
- Other gotchas:
  - Resetting paper invalidates the API key — automate key rotation if you reset programmatically.
  - Crypto orders sometimes "complete" at a price that doesn't match the quote you saw 200ms earlier — assume the spread.
  - Fractional orders are limited to **market** and **day-limit** — no fractional bracket orders on equities.
  - Options data on Basic is "indicative" — not suitable for production options strategies; you'll want OPRA via a paid feed.

---

## PART B — $1/DAY ANTHROPIC BUDGET (April 2026)

### 1. Published prices ($/MTok, verified against docs.claude.com pricing page + cross-checked aggregators)

| Model | Input | Output | Cache write 5m | Cache write 1h | Cache read |
|---|---:|---:|---:|---:|---:|
| Claude Haiku 4.5 | $1.00 | $5.00 | $1.25 | $2.00 | $0.10 |
| Claude Sonnet 4.6 | $3.00 | $15.00 | $3.75 | $6.00 | $0.30 |
| Claude Opus 4.6 | $5.00 | $25.00 | $6.25 | $10.00 | $0.50 |
| Claude Opus 4.7 (released 2026-04-16) | $5.00 | $25.00 | $6.25 | $10.00 | $0.50 |

Multipliers vs. base input: 5m write = 1.25×, 1h write = 2.0×, cache read = 0.1× (90% discount). Batch API = additional **50%** off the entire bill, stackable with caching.

Note: the new Opus 4.7 launched at the **same headline price** as Opus 4.6 — this is unusual and benefits us. Sonnet 4.6 and both Opus tiers ship with a **1M-token context window** at standard pricing (no premium tier).

### 2. Prompt caching mechanics

| Property | Value |
|---|---|
| TTLs available | 5 minutes (default), 1 hour (extended) |
| Discount on hit | 90% off input price |
| Write premium (5m) | +25% on first write |
| Write premium (1h) | +100% on first write |
| Min cacheable size | Sonnet/Opus: 1,024 tokens. Haiku 4.5: **4,096 tokens** (Haiku is harder to cache for small prompts) |
| Breakeven | 5m: pays off after 1 read. 1h: pays off after 2 reads. |
| Cache eviction | Promptly (not instantly) after TTL expiry |

**Important 2026 regression:** Anthropic quietly changed the *default* TTL from 1h to 5m in early 2026 — many production users saw 30–60% bill increases. **Always pass `ttl: "1h"` explicitly** in `cache_control` if you want hourly. (See claude-code GitHub issue #46829.)

For a trading bot polling once every 5–10 minutes during the session, the 5m cache is awkward — the system prompt will frequently expire between calls. **Prefer the 1h cache for system prompt + tool definitions + market context blocks.**

### 3. Batch API

- Same 50% discount as 2024/25 — unchanged.
- Returns within 24h (most batches < 1h in practice).
- Stacks with prompt caching for compounding savings.
- Perfect for **end-of-day reviews**, overnight strategy backtests, daily journal generation, embedding/scoring runs. **Not** suitable for intraday signal generation.

### 4. Token-budget math at $1/day

Cost formula per call:
```
cost = (cache_write_5m_tokens × write_5m_rate
      + cache_read_tokens     × read_rate
      + new_input_tokens      × input_rate
      + output_tokens         × output_rate) / 1,000,000
```

#### Single-call examples (cache-hit case — i.e., you've already paid the write cost earlier in the day)

| Call shape (50K cached read + 5K new input + 2K output) | Haiku 4.5 | Sonnet 4.6 | Opus 4.6/4.7 |
|---|---:|---:|---:|
| Cached read (50K × read rate) | $0.0050 | $0.0150 | $0.0250 |
| New input (5K × input rate) | $0.0050 | $0.0150 | $0.0250 |
| Output (2K × output rate) | $0.0100 | $0.0300 | $0.0500 |
| **Per-call total** | **$0.020** | **$0.060** | **$0.100** |

#### What $1.00 buys you (cache-hit calls of the shape above)

| Model | Calls/day at $1 |
|---|---:|
| Haiku 4.5 | 50 |
| Sonnet 4.6 | 16–17 |
| Opus 4.6/4.7 | 10 |

#### Cache-write cost (paid once per TTL window)

If your shared system prompt + tool defs + market context = **20K tokens** written to 1h cache:

| Model | One-time 1h write cost | Repeated daily? |
|---|---:|---|
| Haiku 4.5 | $0.040 | Yes — refresh every hour during trading session |
| Sonnet 4.6 | $0.120 | Yes |
| Opus 4.6/4.7 | $0.200 | Use sparingly |

A 6.5-hour US trading session needs ~7 hourly cache writes, so amortize: Sonnet 1h cache for the whole day = ~$0.84 just on writes if naive. Better strategy: write once, then re-issue with the same cache key — Anthropic's behavior is that **continued usage within the TTL refreshes/extends the entry effectively for free** beyond the initial premium. Verify this in your own logs.

#### Mixed-model daily mix examples (illustrative)

| Mix | Cost calc | Total |
|---|---|---:|
| 1 Opus + 5 Sonnet + 10 Haiku (all cache-hit shape above) | 0.10 + 5×0.06 + 10×0.02 | **$0.60** |
| 2 Opus + 8 Sonnet + 20 Haiku | 0.20 + 0.48 + 0.40 | **$1.08** ❌ over |
| 1 Opus + 5 Sonnet + 10 Haiku + 1 batch Opus EOD review (50% off, same shape) | 0.60 + 0.05 | **$0.65** |
| 0 Opus + 10 Sonnet + 30 Haiku | 0.60 + 0.60 | **$1.20** ❌ over |

### 5. Recommended cycle structure (fits in $1.00/day for a 4-agent system)

Assumptions: shared 20K cached system+tools (1h TTL), 3–5K fresh market context per call, 1–2K output. US session 09:30–16:00 ET.

```
09:25  Pre-open Opus 4.7 strategy plan        1 call    ~$0.10
       (cold cache write here: +$0.20 once)             ~$0.20
10:30  Sonnet 4.6 mid-morning re-eval         1 call    ~$0.06
12:00  Sonnet 4.6 midday position review      1 call    ~$0.06
13:30  Haiku 4.5 sentiment / news scan        2 calls   ~$0.04
15:00  Sonnet 4.6 power-hour signal           1 call    ~$0.06
15:55  Haiku 4.5 close-the-day check          1 call    ~$0.02
16:30  Opus 4.7 EOD review (BATCH, 50% off)   1 call    ~$0.05
                                              ─────────────────
                                              TOTAL:    ~$0.59
                                              Headroom: ~$0.41
```

That headroom buys ~6 extra Haiku calls or ~7 extra Sonnet calls for ad-hoc agent debates / tool-use chains. Use it for the "competitive" part — let agents critique each other's trades.

For a leaner version (kill the morning Opus on quiet days):
```
1 Sonnet open + 3 Sonnet mid + 5 Haiku scans + 1 batch Opus EOD = ~$0.40
```

For an aggressive day (volatile market, want more Opus reasoning):
```
2 Opus + 4 Sonnet + 6 Haiku = ~$0.20 + $0.24 + $0.12 = $0.56
```

### 6. Honest assessment: is $1/day actually feasible?

**Short answer: yes — but only with discipline. It's tight, not impossible.**

What makes it feasible:
- Prompt caching at 90% off makes the shared system prompt nearly free after first write.
- Batch API at 50% off makes EOD analysis essentially "free" relative to live calls.
- Haiku 4.5 at $1/$5 is genuinely cheap and capable — it can handle 60–70% of the agent calls (sentiment, simple decisions, tool routing).
- Opus 4.7 at $5/$25 is a *bargain* compared to historical Opus pricing — the same $1 buys ~3× more Opus tokens than in 2024.

What kills the budget fast:
- **Long output**. Output tokens are 5× input. Cap `max_tokens` aggressively (1–2K is plenty for a trade decision; reserve 4K+ only for the EOD review).
- **Forgetting to cache.** Every uncached call on Opus with a 25K prompt costs $0.125 — eight of those is your whole day.
- **Tool-use loops.** Each tool call is a round-trip with full context. An Opus agent making 5 tool calls per "decision" can blow $0.50 in one cycle. Use Haiku for tool-heavy chains.
- **5m default TTL silently re-billing writes.** Always specify `ttl: "1h"`.
- **Long thinking / extended reasoning.** Extended thinking output is billed as output tokens; a single Opus extended-thinking burst can cost $0.20+. Disable for routine calls.

Severe constraints to commit to:
1. **Hard `max_tokens` ceilings**: Haiku 256–1024, Sonnet 1024–2048, Opus 2048–4096. Enforce in code.
2. **One Opus call per cycle, max two per day** during dev. Sonnet is the workhorse.
3. **Cache or die.** No call > 2K input tokens may be uncached.
4. **No streaming live orderbook into prompts.** Summarize every market snapshot to ≤500 tokens before it hits the model.
5. **Local guardrail**: maintain a running `daily_spend.json` that the agent checks before every call — refuse to call if `today_spent + estimated_cost > $0.95`.
6. **Batch everything that isn't intraday**: backtests, performance reports, journal entries, prompt-tuning experiments — all go through `/v1/messages/batches`.
7. **Use Haiku as a "router"**: a $0.005 Haiku call decides whether the question is worth a $0.10 Opus call.

Realistic verdict for a 4-agent competitive system:
- **3-agent live + 1 batch reviewer** is comfortable at $0.50–0.70/day.
- **4 fully-live agents** (Haiku + Sonnet + Opus-4.6 + Opus-4.7 all calling intraday) is achievable but has zero margin for error — one runaway tool loop blows the day.
- **Truly meaningful reasoning?** Yes for Sonnet/Opus on 1–3 decision points per day. No, you cannot have all four agents "thinking deeply" continuously — that requires $5–10/day minimum.

The dollar-cap forces good architecture. Anything you'd build at $1/day will scale gracefully when you raise the cap to $10 or $100 for live trading. Treat the budget as an architectural feature, not a limitation.

---

## Sources

- [Claude API Pricing — platform.claude.com](https://platform.claude.com/docs/en/about-claude/pricing)
- [Claude Prompt Caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Anthropic Batch API docs](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
- [Finout: Anthropic API Pricing in 2026](https://www.finout.io/blog/anthropic-api-pricing)
- [pecollective: Claude API Pricing 2026](https://pecollective.com/tools/anthropic-api-pricing/)
- [evolink.ai: Claude API Pricing Guide 2026](https://evolink.ai/blog/claude-api-pricing-guide-2026)
- [aicheckerhub: Anthropic Prompt Caching 2026 Cost & TTL guide](https://aicheckerhub.com/anthropic-prompt-caching-2026-cost-latency-guide)
- [claude-code Issue #46829: Cache TTL regression](https://github.com/anthropics/claude-code/issues/46829)
- [Alpaca Paper Trading docs](https://docs.alpaca.markets/docs/paper-trading)
- [Alpaca Trading API overview](https://docs.alpaca.markets/docs/trading-api)
- [Alpaca Fractional Trading docs](https://docs.alpaca.markets/docs/fractional-trading)
- [Alpaca Options Trading docs](https://docs.alpaca.markets/docs/options-trading)
- [Alpaca Options Trading Overview](https://docs.alpaca.markets/docs/options-trading-overview)
- [Alpaca Multi-Leg (Level 3) Options blog](https://alpaca.markets/blog/level-3-options-trading-now-available-with-alpacas-trading-api/)
- [Alpaca Crypto Spot Trading Fees](https://docs.alpaca.markets/docs/crypto-fees)
- [Alpaca Crypto Spot Trading docs](https://docs.alpaca.markets/docs/crypto-trading)
- [Alpaca Working with Orders](https://docs.alpaca.markets/docs/working-with-orders)
- [Alpaca Bracket Orders blog](https://alpaca.markets/blog/bracket-orders/)
- [Alpaca Market Data API overview](https://docs.alpaca.markets/docs/about-market-data-api)
- [Alpaca WebSocket Stream docs](https://docs.alpaca.markets/docs/streaming-market-data)
- [Alpaca rate-limit support article](https://alpaca.markets/support/usage-limit-api-calls)
- [Alpaca PDT support article](https://alpaca.markets/support/what-is-the-pattern-day-trading-pdt-rule)
- [Alpaca User Protection docs](https://docs.alpaca.markets/docs/user-protection)
- [alpaca-py GitHub repo](https://github.com/alpacahq/alpaca-py)
- [Alpaca SDKs and Tools](https://docs.alpaca.markets/docs/sdks-and-tools)
- [Alpaca Status page](https://status.alpaca.markets/)
- [StatusGator Alpaca incident history](https://statusgator.com/services/alpaca)
