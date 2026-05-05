# Institutional Investment Strategies for an LLM-Driven Multi-Agent Trading System

**Author:** Research compiled for the three-Claude-agent (+ manager) Alpaca paper-trading project
**Horizon:** Daily-to-weekly rebalancing, $1,000 sub-portfolios, beat S&P 500 net of taxes/API costs
**Date:** April 2026

---

## 1. Why this matters for an LLM advisor

The bar an LLM-driven advisor must clear is brutally honest: the median active equity manager fails to beat the S&P 500 over 10+ years, *with infrastructure, data feeds, and PhDs*. With only $1,000 per agent, a daily-to-weekly cadence, and reasoning latency measured in seconds (not microseconds), the realistic edge for Claude is **process discipline plus information synthesis**, not speed. The strategies below are evaluated through that lens.

---

## 2. Core institutional strategies

### 2.1 Factor investing (Fama-French / AQR style)

**Thesis.** Persistent return premia exist for stocks sorted on observable characteristics: **value** (cheap on P/B, P/E, EV/EBITDA), **momentum** (12-month minus 1-month return), **quality** (high ROE, stable earnings, low leverage), **low volatility**, and **size**. AQR's "Fact, Fiction, and Factor Investing" and Asness's body of work argue these survive transaction costs over multi-decade periods.

**Edge.** Per AQR data, the quality premium delivered ~4.7% annual excess return with a Sharpe of ~0.47 (1964-2023). Value + profitability blended hits Sharpe ~0.58. A 60/40 value-momentum combo improves on either alone because the two factors are negatively correlated. Drawdowns are real: value endured a 13-year drawdown 2007-2020.

**Inputs.** Fundamentals (P/B, ROE, accruals), trailing 12-month returns, daily volatility. All available free via Alpaca, Yahoo, Finviz, or SEC EDGAR.

**Holding period.** Monthly to quarterly rebalance is standard; weekly is acceptable with minor turnover drag.

**LLM-implementable?** **Yes, very.** Factor scoring is rules-based; the LLM's role is robust ranking, sanity-checking outliers, and writing a defensible weekly rationale. No HFT infrastructure needed.

**$1,000 suitability.** Excellent. Use 5-15 fractional-share positions on liquid US large/mid caps. PDT rule does not bite if held >1 day.

---

### 2.2 Trend following / managed futures (time-series momentum)

**Thesis.** Moskowitz, Ooi & Pedersen (2012) "Time Series Momentum" showed that across 58 futures contracts (equities, bonds, commodities, FX), the past 12-month return of an *individual* asset positively predicts its next 1-12 month return. A diversified TSMOM portfolio earns Sharpe >1.0 historically with low correlation to passive benchmarks; AQR's "Demystifying Managed Futures" attributes most CTA returns to this single signal.

**Edge.** Sharpe 0.7-1.2 in published studies; positive in 90% of asset-decade samples. Famously profitable in 2008 and 2022 when equity-bond correlation broke. Major drawdowns occur in choppy/range-bound markets (2011-2013, 2018).

**Inputs.** Daily price data only.

**Holding period.** Weekly to monthly trend signal, with vol-targeting overlay.

**LLM-implementable?** **Yes**, but with a caveat: futures aren't accessible on Alpaca paper for retail; the LLM must implement TSMOM via **ETF proxies** (SPY, TLT, GLD, USO, UUP, EEM, IWM, etc.). Performance degrades vs. true futures TSMOM but retains the core signal.

**$1,000 suitability.** Very good. ~6-10 ETFs, equal-vol-weighted with cash buffer when no asset is in uptrend.

---

### 2.3 Cross-sectional momentum (equity)

**Thesis.** Jegadeesh & Titman (1993) showed buying the prior 3-12 month winners and shorting losers earns ~1% per month. Skip the most recent month to avoid short-term reversal. AQR's "Value and Momentum Everywhere" generalizes the effect across asset classes and geographies.

**Edge.** Long-only top-decile vs. market: ~3-5% annualized excess. Long-short Sharpe 0.5-0.8 historically, but momentum crashes (2009, 2016) cost 30-50% in weeks.

**Inputs.** Trailing returns, market cap filter (skip microcaps).

**Holding period.** 1-3 month rebalance is institutional standard; weekly works with discipline.

**LLM-implementable?** **Yes.** Pure ranking exercise.

**$1,000 suitability.** Long-only version is fine. Short-leg requires margin/borrow that Alpaca paper supports but is impractical at this size.

---

### 2.4 Mean reversion / statistical arbitrage / pairs trading

**Thesis.** Cointegrated pairs (e.g., KO/PEP, XOM/CVX, GLD/IAU) drift apart short-term and revert. Trade the spread when it exceeds ~2 standard deviations.

**Edge.** Gatev, Goetzmann & Rouwenhorst (2006) reported ~11% annualized from 1962-2002. Largely arbitraged out post-2003 in liquid US large caps; ETF pairs studies still show Sharpe 0.8-1.2 in selected baskets.

**Inputs.** Daily prices, rolling cointegration test (Engle-Granger or Johansen).

**Holding period.** Days to weeks.

**LLM-implementable?** **Marginal.** True stat-arb is the domain of Renaissance/Two Sigma running thousands of pairs at sub-second latency. An LLM can supervise a *handful* of named pairs and act on extreme-z-score alerts, but the edge is thin and slippage-sensitive. Better as a small overlay than a core strategy.

**$1,000 suitability.** Capital-inefficient (each pair needs both legs); short-selling fees and PDT exposure if you flip frequently.

---

### 2.5 GARP (Growth at a Reasonable Price)

**Thesis.** Peter Lynch's hybrid: companies with above-market earnings growth trading at PEG <1 (sometimes <1.5). Combines value's discount discipline with growth's compounding.

**Edge.** Lynch's Magellan ran 29.2% CAGR 1977-1990 vs. ~15.8% for the S&P. CFA Institute studies show ~38% of stocks have PEG <1 at any time, so it's a screen not a free lunch. Modern GARP funds cluster around market-plus-1-3% with lower drawdowns than pure growth.

**Inputs.** Forward EPS estimates (Yahoo, Finviz, Alpaca news), trailing growth rates, P/E.

**Holding period.** Quarterly to annual; tolerates daily monitoring well.

**LLM-implementable?** **Yes, ideally.** GARP is fundamentally narrative-plus-numbers, exactly what an LLM does best: read 10-Q commentary, score management quality, rank by PEG, validate the growth thesis. Lynch himself was discretionary, not quant.

**$1,000 suitability.** Excellent: 5-10 names, low turnover, no PDT issue.

---

### 2.6 Macro / Global Tactical Asset Allocation (GTAA)

**Thesis.** Meb Faber's "A Quantitative Approach to Tactical Asset Allocation" rotates between global asset-class ETFs based on whether each is above its 10-month SMA. Discretionary macro (Bridgewater, Brevan Howard) instead reads central bank policy, fiscal stance, and growth/inflation regime.

**Edge.** Faber's GTAA: ~10-12% annualized 1973-2025 with Sharpe ~0.6 and max drawdown ~20% vs. ~50% for buy-and-hold. Discretionary macro Sharpe is highly manager-dependent (0.5-0.9 historically).

**Inputs.** Monthly closes for ~5-13 ETFs (US equity, intl equity, REITs, commodities, bonds), plus optional macro reads (yield curve, ISM, jobless claims).

**Holding period.** Monthly rebalance is the canonical implementation.

**LLM-implementable?** **Yes, very well.** Faber-style is trivially rules-based. The LLM adds value by overlaying narrative macro reads ("Fed paused, oil up 8% on Middle East", etc.) onto the trend signal.

**$1,000 suitability.** Excellent.

---

### 2.7 Risk parity (Bridgewater All Weather style)

**Thesis.** Equal-risk-weight across asset classes, then apply leverage to hit return target. Stocks/bonds/commodities/TIPS each contribute equal vol.

**Edge.** Historically ~8.4% return with ~11% vol and Sharpe ~0.43, but the strategy depends on negative stock-bond correlation. 2022 saw correlation spike to ~+0.65 and risk parity funds suffered double-digit losses.

**Holding period.** Quarterly rebalance.

**LLM-implementable?** **Yes**, but **not at $1,000 without leverage** the strategy is meant to use. Without futures or margin, the unlevered version basically becomes a 60/40 lookalike.

**$1,000 suitability.** Poor. Skip in favor of GTAA.

---

### 2.8 Long/short equity

**Thesis.** Long undervalued, short overvalued; net market exposure 30-70%; alpha from stock selection, not direction.

**Edge.** Top-tier long/short funds run Sharpe 0.8-1.2; median is lower. Man Group and AQR papers stress factor neutralization (sector, beta, size) so alpha is genuinely idiosyncratic.

**LLM-implementable?** Conceptually yes. Practically the short leg costs borrow, has unlimited downside, and triggers regulatory friction.

**$1,000 suitability.** **Avoid.** Use long-only with cash buffer instead.

---

### 2.9 Options strategies for small accounts

Three are realistic on Alpaca paper (which supports level-2/3 options):

- **Covered calls.** Own 100 shares (or fractional-equivalent on cheap ETFs), sell 30-45 DTE calls at delta ~0.30. Adds ~0.5-1% monthly income; cost is capped upside. Requires $1,000+ in a single name to write 100-share lots, so practically limited to <$10 underlyings.
- **Cash-secured puts (CSP).** Sell puts on stocks you'd be willing to own at strike. Premium ~0.5-1.5% per month; downside is full ownership at strike. Combined with CCs, this is the **"Wheel Strategy"** popular with retail.
- **Defined-risk verticals (credit spreads, iron condors).** Best for accounts <$25K because max loss is capped at spread width minus credit. Iron condor on SPY at 1-SD strikes, 30-45 DTE, targets 5-10% return on risk. Vulnerable to gap events.

**LLM-implementable?** **Yes**, with strict guardrails. Options pricing is well understood; the LLM should choose strikes/expirations from a screened menu, not free-form. Volatility regime detection (VIX level, term structure) is the high-value LLM input.

**$1,000 suitability.** Mixed. CCs/CSPs need ≥$3-5K to be useful on quality names; verticals work at $1K but commissions and bid-ask eat returns.

---

## 3. Investment-bank stock-picking frameworks

Sell-side equity research at GS / MS / JPM / Barclays generates Buy/Hold/Sell ratings with 12-month price targets via a combination of:

- **DCF.** Forecast 5-10y free cash flows, terminal value (Gordon growth or exit multiple), discount at WACC. Most defensible but extremely sensitive to terminal assumptions; rarely the *headline* methodology outside high-growth tech and large caps.
- **Comparables (trading multiples).** Apply peer-median P/E, EV/EBITDA, EV/Sales, P/AFFO (REITs), P/BV (banks). Industry-specific: P/E dominates 20 of 25 industries; EV/EBITDA in telecom/energy/materials; P/AFFO in REITs (per academic studies of analyst practice).
- **Sum-of-the-parts (SOTP).** For conglomerates (BRK, GE, AMZN), value each segment by its own multiple and aggregate.
- **Catalyst maps.** Earnings dates, FDA decisions, M&A rumors, regulatory rulings, capital-markets days. Ratings often shift on **expected** catalysts, not realized fundamentals.
- **Sector rotation playbook.** Top-down call on cycle phase (early/mid/late/recession) maps to GICS sector tilts: early-cycle = consumer discretionary/financials/industrials; late = energy/materials/staples; recession = healthcare/staples/utilities. Note: academic work (Molchanov 2024) finds cycle-driven sector rotation produces *modest* outperformance pre-costs and frequently negative post-costs.

**LLM mapping.** An LLM is well-suited to *replicate* the qualitative side of sell-side reports — read the 10-Q, write the bull/bear case, identify catalysts, list comparables. It cannot reliably build precise DCFs unless given structured inputs, and it must not pretend it can.

---

## 4. Risk-management essentials any serious bot must implement

### 4.1 Position sizing

- **Fixed fractional** (1-3% of equity per trade) is the default and what most retail systems should use. Balsara (1992) showed it produces materially lower max drawdowns than Kelly.
- **Kelly criterion** = (edge/odds). Optimal *if* you know your true win rate and payoff. You don't. Full Kelly produces 50%+ drawdowns even with positive expectancy. Use **Half- or Quarter-Kelly** at most.
- **Volatility targeting.** Size each position to contribute equal portfolio vol (e.g., risk per trade = 1% × equity / ATR). This is the modern institutional default and what the LLM should use.

### 4.2 Stops

- **Hard stop** at 2× ATR(14) or -8% from entry, whichever is closer.
- **Trailing stop** at 2.5× ATR for trend-followers.
- **Time stop** (close after N days if thesis hasn't played out) reduces dead capital.
- **Catalyst stops** (close before earnings unless thesis is the earnings).

### 4.3 Portfolio-level

- Cap single-name weight at 10-15%.
- Cap sector weight at 30%.
- Cap portfolio beta (vs. SPY) at 1.2 net.
- Monitor pairwise correlation; flag if median pairwise >0.7 (concentration risk).
- **Drawdown circuit breaker:** if portfolio drops -10% from peak, halve position sizes; -15% pause new entries; -20% liquidate to cash and force a written postmortem.

### 4.4 Tax & wash-sale

The wash-sale rule disallows a loss if a "substantially identical" security is bought within 30 days before or after the sale (61-day window total), per IRS / Schwab / Fidelity guidance. A bot that day-trades the same ticker repeatedly will generate dozens of wash sales — disallowed losses get added to cost basis but defer the deduction. Practical rules:

- Do not re-enter a closed-loss position for 31 calendar days.
- For tax-loss harvesting, swap to a non-identical proxy (SPY → IVV is risky; SPY → RSP is safe).
- This is paper trading, so it's pedagogical, but the manager agent should still produce a realistic *as-if-taxable* P&L.

### 4.5 PDT rule

Per Alpaca docs: 4+ day-trades in 5 business days where day-trades are >6% of total trades flags the account as a Pattern Day Trader; under $25K equity, the 4th day trade is **rejected with HTTP 403** in paper too. At $1,000, this hard-caps each agent at 3 day trades per rolling 5-day window. **Therefore the strategy mix must be biased to overnight holds.**

---

## 5. Honest verdict — what an LLM can and cannot do

### What Claude can plausibly do well
- **Synthesize a daily briefing** (price action + news + earnings + macro) and translate into a ranked watchlist.
- **Apply rules-based factor screens** consistently and explain the decision.
- **Read 10-Qs / earnings transcripts** and update a thesis (the GARP and sell-side workflows).
- **Detect regime shifts** in qualitative terms (Fed pivot, geopolitical shock) and tilt the GTAA signal.
- **Maintain risk discipline** (size, stops, sector caps) more reliably than a discretionary human, because the LLM doesn't get bored or revenge-trade.

### What Claude cannot do
- **Compete on speed.** No HFT, no order-flow analysis, no microstructure edges.
- **Run true stat-arb at scale.** That's a thousands-of-pairs, sub-second business.
- **Forecast returns.** No model does this reliably; the LLM should not pretend to.
- **Replace a real risk system.** The deterministic Python rules (vol-target, stops, PDT counter) must be enforced *outside* the LLM, not by it.

---

## 6. Ranked recommendation — strategies the four agents should specialize in

The best architecture pairs **complementary, low-correlated strategies** so the manager agent can blend them. Recommended specialization:

| Agent | Strategy | Why it fits |
|---|---|---|
| **Haiku** (fastest, cheapest) | **Faber GTAA + ETF momentum** (5-10 ETF universe, weekly trend signal) | Rules-heavy, low token cost per decision, ETF universe avoids PDT, low turnover. Plays the role of disciplined trend follower. |
| **Sonnet** (balanced) | **Multi-factor equity (value + momentum + quality)** on liquid US large/mid caps, 10-15 names, monthly rebalance | Sweet spot for Sonnet: enough reasoning bandwidth to rank, sanity-check, and write rationales; cheap enough to run nightly. Highest expected long-run Sharpe of the three. |
| **Opus** (deepest) | **GARP / sell-side discretionary** — 5-8 high-conviction names with full thesis, catalyst calendar, and 10-Q reads | Opus's reasoning depth is wasted on mechanical screens; deploy it where qualitative synthesis dominates. Closest to a junior PM at a long-only mutual fund. |
| **Manager** | **Allocator + risk overseer** — sets capital weights between the three, enforces portfolio caps, runs the drawdown circuit breaker, generates the unified weekly report | Adds a meta-layer that blends signals and prevents any one agent from blowing up the aggregate. |

**Optional fourth strategy** (to be assigned to whichever agent has spare cognitive budget after backtesting): **covered-call / wheel overlay** on the most-stable holdings in the multi-factor or GARP sleeves. Adds ~0.5-1%/month income with limited upside cost — a clean fit for Alpaca's options API at the $1K scale.

**Strategies to explicitly avoid**: pure stat-arb (infrastructure mismatch), risk parity (needs leverage), long/short with shorts (cost and complexity), intraday day-trading (PDT blocks it), naked options (catastrophic tail risk).

---

## 7. Sources

- AQR Capital Management — *Fact, Fiction, and Factor Investing*; *Value and Momentum Everywhere*; *Demystifying Managed Futures*; *Quality Factor Strategies*. https://www.aqr.com
- Moskowitz, Ooi & Pedersen (2012). *Time Series Momentum*. Journal of Financial Economics.
- Jegadeesh & Titman (1993). *Returns to Buying Winners and Selling Losers*. Journal of Finance.
- Faber, M. (2007/2013). *A Quantitative Approach to Tactical Asset Allocation*. SSRN 962461.
- Bridgewater Associates — *The All Weather Story / The All Weather Strategy*. https://www.bridgewater.com
- Gatev, Goetzmann & Rouwenhorst (2006). *Pairs Trading: Performance of a Relative-Value Arbitrage Rule*. Wharton.
- Ball & Brown (1968) and subsequent PEAD literature; UCLA Anderson Review (2024); Philadelphia Fed WP 21-07.
- CFA Institute — *GARP Investing: Golden or Garbage?*; analyst valuation practice studies.
- Damodaran, A. — *Growth Investing: GARP* lecture notes, NYU Stern.
- Fidelity / Schwab / J.P. Morgan — Wash-sale and tax-loss harvesting guidance.
- Alpaca Markets docs — *User Protection / PDT Rule*. https://docs.alpaca.markets
- Man Group / Two Sigma / Resonanz Capital — GenAI in hedge funds (2024-2025 commentary).
- Optionalpha, OptionStrat, Fidelity Learning Center — covered call, CSP, iron condor mechanics.
- Molchanov (2024). *The myth of business cycle sector rotation*. International Journal of Finance & Economics.

---
*End of document. Word count ~2,650.*
