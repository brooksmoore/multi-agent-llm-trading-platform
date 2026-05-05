# 07 — Leverage Strategies for Autonomous LLM Trading Agents

*Research compiled 2026-04-24 for the Multi-Agent Asset Competitive Bot project.*
*Audience: Brooks. Goal: institutional-grade leverage logic for the four-agent paper-trading system on Alpaca, with a `MASTER_CAPABILITY` lever that scales risk cleanly across the Haiku/Sonnet/Opus sub-portfolios under the Manager (CIO).*

---

## Section 1 — How professional shops actually use leverage

### 1.1 Hedge funds (long/short equity, global macro, multi-strat)

Aggregate data from the Office of Financial Research's Hedge Fund Monitor and the Ang/Gorovyy NBER paper ("Hedge Fund Leverage", NBER 16801) gives reasonable order-of-magnitude anchors:

- **Long/short equity**: average gross exposure ~1.6x NAV, net exposure typically 30–60% long-bias. Most "fundamental" L/S books run 130/70 to 180/80 (gross 200–260%).
- **Event-driven**: average gross ~1.3x — leverage is low because catalyst-dependent positions don't stack diversification well.
- **Global macro**: highly variable; can reach 4–8x notional via futures and FX, but volatility-targeted, so cash-equivalent leverage is more modest.
- **Multi-manager / pod shops** (Citadel, Millennium, Point72, Balyasny, Schonfeld): gross often 400–700%, net near zero. Each pod gets a tight VaR/stop budget; gross is high because pods are uncorrelated and pod-level drawdown stops fire long before fund-level damage.

The institutional mental model isn't "leverage = aggression". It's **leverage = (target risk) / (per-unit risk of the strategy)**. A market-neutral pod with 4% standalone vol needs 5x gross to deliver 20% vol; a concentrated long-biased equity fund with 25% standalone vol needs ~0.6x gross to do the same. Prime-broker covenants (typically a multiple of net asset value plus regulatory minimums) are the hard ceiling.

Sources: [OFR Hedge Fund Monitor — Leverage by Strategy](https://www.financialresearch.gov/hedge-fund-monitor/categories/leverage/chart-27/); [Ang & Gorovyy, "Hedge Fund Leverage", NBER 16801](https://www.nber.org/system/files/working_papers/w16801/w16801.pdf).

### 1.2 CTAs / managed futures (AQR, Man AHL, Winton, Aspect)

CTAs are **the** practitioners of volatility targeting. Key elements:

- **Target portfolio vol ~10%** at the fund level (AQR's Managed Futures Strategy targets ~10%; the HV variant targets ~15%). The "standard" institutional vol target band is 10–15%.
- **Equal risk contribution across asset classes** — each of equities/fixed income/currencies/commodities gets ~25% of the risk budget, then time-series momentum signals scale long/short within each.
- **60-day rolling vol estimate** is AQR's default lookback for sizing (per "Demystifying Managed Futures"); many shops use EWMA with λ ≈ 0.94 (RiskMetrics standard, ~20-day half-life).
- **Daily rebalance** of position vol; weekly rebalance of strategy weights. Trend-following Sharpes hover around 0.6–0.8 standalone but exhibit *crisis alpha* (positive convexity in equity drawdowns).
- **Leverage scales without changing Sharpe** — a clean property of trend strategies. Doubling leverage doubles vol and (gross of fees) doubles return.

Sources: [AQR — Demystifying Managed Futures](https://www.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/Demystifying-Managed-Futures.pdf); [The Hedge Fund Journal — Quantifying CTA Risk Management](https://thehedgefundjournal.com/quantifying-cta-risk-management/); [Sepp — Trend-following strategies for tail-risk hedging](https://artursepp.com/wp-content/uploads/2018/04/Trend-following-strategies-for-tail-risk-hedging-and-alpha-generation.pdf).

### 1.3 Risk parity (Bridgewater All Weather, AQR Risk Parity)

Risk parity equalizes *risk contribution* (not capital) across asset classes. Because bonds have ~1/3 the volatility of equities, you must **lever bonds ~2–3x** to balance their risk weight against stocks. Bridgewater's All Weather, as wrapped in the State Street ALLW ETF, runs ~1.8x notional leverage, with bond-equivalent exposure pushing well above 1x.

The 2022 case study is the canonical risk-parity stress test. Stock-bond correlation flipped from a long-run ~−0.2 to ~+0.65 as the Fed hiked. All Weather lost ~22% in 2022 — its worst year on record, worse than 2008. The lesson: **risk parity assumes diversification benefit holds in stress**. When correlations regime-shift, levered bond exposure compounds the equity loss instead of offsetting it.

Sources: [Bridgewater — The All Weather Story](https://www.bridgewater.com/research-and-insights/the-all-weather-story); [State Street ALLW factsheet](https://www.ssga.com/us/en/intermediary/etfs/spdr-bridgewater-all-weather-etf-allw); [Markov Processes — Risk Parity Not Performing? Blame the Weather](https://www.markovprocesses.com/blog/risk-parity-not-performing-blame-the-weather/); [CAIA — Risk Parity Not Performing?](https://caia.org/blog/2024/01/02/risk-parity-not-performing-blame-weather).

### 1.4 Prop trading desks / market makers

Prop and market-making desks live inside **hard VaR limits** allocated by the firm-level CRO. The Federal Reserve's 2025 paper on dealer Treasury desks (FEDS 2025-034) shows dealers cut positions sharply as they approach internal VaR caps — limits are economically costly to breach, not just symbolic.

Typical features:
- **VaR allocated as a budget**: e.g. desk gets $10M of 99% 1-day VaR; if used, desk is full and must trim before adding.
- **Intraday vs overnight risk** are separate budgets — overnight is more restrictive because gap risk is uncovered.
- **Stress tests run nightly**: 1987-style equity crash, 2008-style credit shock, 2020-style liquidity vacuum, 2022-style correlation flip. Positions must survive scenario losses below a hard cap.
- **Procyclicality of VaR** is a known problem — when realized vol drops, VaR drops, capacity increases, exposure builds, and a vol spike then forces fire-sale deleveraging. This is the volatility paradox in microcosm.

Sources: [Federal Reserve — The Role of Trading Desk Risk Limits, FEDS 2025-034](https://www.federalreserve.gov/econres/feds/files/2025034pap.pdf); [QuantStart — VaR for Algorithmic Trading](https://www.quantstart.com/articles/Value-at-Risk-VaR-for-Algorithmic-Trading-Risk-Management-Part-I/).

### 1.5 Long-only mutuals with limited leverage (130/30, leveraged ETFs)

The 130/30 (or "active extension") category is the institutional acknowledgement that *modest leverage on a high-information-ratio strategy beats higher leverage on a weak one*. Acadian and Wellington's research ([Acadian: Systematic 130/30](https://www.acadian-asset.com/-/media/files/thematic-research-paper-pdfs/acadian--13030-extension-strategies.pdf); [Wellington: 140/40](https://www.wellington.com/en-us/institutional/insights/fundamental-extension-strategies)) consistently shows that relaxing the long-only constraint to 130/30 captures most of the available alpha extension; going to 200/100 adds risk faster than information ratio.

This is the cleanest argument against the "more leverage = more alpha" intuition: alpha extension has diminishing marginal returns, while leverage has linear marginal cost (financing) and convex marginal risk (drawdown).

---

## Section 2 — Mathematical frameworks for sizing leverage

### 2.1 Volatility targeting

Canonical formula:

```
position_size_t = (target_vol / realized_vol_t) * notional
```

For a multi-asset portfolio, you target portfolio vol after combining covariances. Realized vol is typically estimated with **EWMA, λ=0.94 (RiskMetrics, ~20-day half-life)**, or a 60-day rolling sample stdev. AQR uses 60-day; faster strategies use shorter (Quantpedia and Portfolio Optimizer note 10-day lookbacks empirically beat 22- and 100-day for short-horizon vol targeting).

For a **$3K paper portfolio aiming to beat SPY** (SPY annual vol ~16% over 2020–2025), the right target depends on the *information ratio you believe in*. If you believe IR ≈ 0.5, target vol equal to SPY (~15–16%). If you're more honest about LLM-driven decision quality (IR likely 0.0–0.3), then target vol slightly below SPY (~10–12%) so you don't pay for the privilege of more drawdown.

**Drawdown interaction**: a vol-targeted strategy with annualized vol σ has expected max drawdown roughly 2–3x σ over multi-year horizons. 15% target vol → expect 30–45% drawdowns at some point. A user with strong loss aversion should target lower.

Sources: [Portfolio Optimizer — Volatility Forecasting](https://portfoliooptimizer.io/blog/volatility-forecasting-simple-and-exponentially-weighted-moving-average-models/); [Quantpedia — Introduction to Volatility Targeting](https://quantpedia.com/an-introduction-to-volatility-targeting/); [Research Affiliates — Harnessing Volatility Targeting](https://www.researchaffiliates.com/content/dam/ra/publications/pdf/1014-harnessing-volatility-targeting.pdf).

### 2.2 Kelly and fractional Kelly

Single-asset Kelly: `f* = (μ − r) / σ²` where μ is expected return, r risk-free, σ² variance.

For a strategy with Sharpe S, geometric-growth-optimal leverage is `L* = S / σ`, giving `g* = S²/2`. Two consequences institutions live by:

1. **Full Kelly produces brutal drawdowns**. Lognormal property: probability of dropping to fraction x of starting capital ≈ x. A full-Kelly strategy with 50% probability hits a 50% drawdown at some point. Most humans (and algorithms with stops) cannot survive this psychologically or operationally.
2. **The growth curve is flat near the optimum**. Half-Kelly captures ~75% of full-Kelly geometric growth with ~half the variance. Quarter-Kelly captures ~44% growth with ~quarter the variance. **Quarter to half-Kelly is institutional standard** for exactly this reason.

Add **estimation error** (you don't know μ or σ) and the case for fractional Kelly tightens further. Lopez de Prado's risk-constrained Kelly literature (and the Busseti/Ryu/Boyd Stanford paper on risk-constrained Kelly) formalizes this: introduce a drawdown or VaR constraint, optimize growth subject to it, and the optimal leverage drops by 50–75% versus unconstrained Kelly.

Sources: [Kelly criterion (Wikipedia)](https://en.wikipedia.org/wiki/Kelly_criterion); [Busseti, Ryu, Boyd — Risk-Constrained Kelly Gambling](https://stanford.edu/~boyd//papers/pdf/kelly.pdf); [Why Fractional Kelly?](https://matthewdowney.github.io/uncertainty-kelly-criterion-optimal-bet-size.html); [QuantInsti — The Risk-Constrained Kelly Criterion](https://blog.quantinsti.com/risk-constrained-kelly-criterion/).

### 2.3 Risk parity scaling

Equal risk contribution: choose weights w such that `w_i * (Σw)_i` is equal across assets. Since contribution is `w_i * σ_i * ρ_i,p`, low-vol assets get larger w to equalize risk — hence the natural lever-up of bonds.

For a long-only $3K paper portfolio in a single sleeve, true risk parity is overkill. But the *spirit* is useful: **size positions inverse to recent vol**, so a 40% vol biotech doesn't dominate the book just because conviction is high.

### 2.4 Sharpe-ratio-based leverage (Markowitz / Black-Treynor-Mazuy)

Markowitz capital market line says optimal leverage scales with Sharpe ratio: better strategy ⇒ more leverage. Practically: **don't fix leverage; fix target vol, then leverage backs out.** A Sharpe-1.0 strategy at 10% standalone vol gets levered to deliver, say, 12% portfolio vol; a Sharpe-0.3 strategy at the same standalone vol should not be levered at all.

The corollary: **the agent that proves the highest Sharpe (after enough sample) deserves the most capital and the most leverage**. This is the natural Manager (CIO) reallocation lever.

### 2.5 Drawdown-aware leverage

Two well-developed literatures:

- **Risk-constrained Kelly** (Busseti/Ryu/Boyd; Lopez de Prado): introduce a constraint of the form `P(max_drawdown > D) ≤ ε` and re-solve for optimal leverage. Result is invariably 0.25–0.5× full Kelly.
- **CPPI (Constant Proportion Portfolio Insurance)**: dynamic leverage `Exposure = m × (Value − Floor)` where m is a multiplier (typically 3–5) and Floor is a minimum acceptable value. As portfolio rises, exposure grows; as it falls, exposure shrinks. **Ratchet variants** lock in gains by raising the floor as the portfolio hits new highs.

CPPI's drawdown-responsive logic translates cleanly into a paper-trading rule: **as drawdown deepens, mechanically reduce leverage**. This is the single most important defense against runaway losses driven by an over-confident LLM.

Sources: [QuantPedia — Introduction to CPPI](https://quantpedia.com/introduction-to-cppi-constant-proportion-portfolio-insurance/); [AXA IM — Understanding Portfolio Insurance Management (CPPI/TIPP)](https://core.axa-im.com/investment-strategies/multi-asset/insights/understanding-portfolio-insurance-management-cppitipp); [CPPI on Wikipedia](https://en.wikipedia.org/wiki/Constant_proportion_portfolio_insurance).

### 2.6 The volatility paradox

Coined in academic risk literature and operationalized by the OFR's 2017 *Tranquil Markets May Harbor Hidden Risks* note: prolonged low realized vol leads investors to (a) lever up inside VaR-budget systems, (b) reduce hedges as carry costs sting, and (c) implicitly assume the world is calm. The system becomes *more* fragile.

**Volmageddon (Feb 5, 2018)** is the textbook case. After a year of suppressed vol (VIX often <11 in 2017), short-vol products ballooned. The S&P fell 4.1% on Feb 5; VIX jumped 115% in one day; XIV lost 97% of NAV; SVXY lost 91%. The mechanical rebalancing of these levered products — "buy more vol when vol rises" — was a self-reinforcing loop.

The lesson for our system: **low VIX is a warning, not a green light**. Lever down when vol is unusually low *or* unusually high; reach maximum capacity in normal regimes.

Sources: [OFR — The Volatility Paradox: Tranquil Markets May Harbor Hidden Risks](https://www.financialresearch.gov/financial-markets-monitor/files/OFR-FMM-2017-08-17_Volatility-Paradox.pdf); [CFA Institute — Volmageddon and the Failure of Short Volatility Products (Augustin et al.)](https://rpc.cfainstitute.org/research/financial-analysts-journal/2021/volmageddon-failure-short-volatility-products); [Six Figure Investing — What Caused the Volatility Volmageddon](https://www.sixfigureinvesting.com/2019/02/what-caused-the-february-5th-2018-volatility-spike-xiv-termination/).

---

## Section 3 — Leverage instruments available to a $3K Alpaca paper account

### 3.1 Reg-T margin (Alpaca)

Per [Alpaca's margin docs](https://docs.alpaca.markets/docs/margin-and-short-selling) and [paper trading docs](https://docs.alpaca.markets/docs/paper-trading):

- All accounts open as **margin accounts** by default.
- **Initial margin**: 2:1 on marginable equities (50% IM under Reg T).
- **Maintenance margin**: 25% (FINRA minimum).
- **Day-trading buying power = 4 × (last_equity − last_maintenance_margin)** for accounts flagged as Pattern Day Trader (PDT) with >$25K equity.
- Paper accounts **simulate PDT checks**: a fourth day-trade in a 5-business-day window is rejected if real-time net worth is below $25K. Our $3K paper portfolios are subject to PDT.
- Practical implication: we have **2:1 overnight buying power** (so $3K → $6K notional) but **no 4:1 day-trading power** because we're under $25K.

### 3.2 Portfolio margin

Requires $100K+ equity (FINRA Rule 4210). **Not available to us.** Mention only for completeness.

### 3.3 Leveraged ETFs (TQQQ, SQQQ, UPRO, SPXU, SOXL, TMF, etc.)

These deliver 2x or 3x **daily** returns on the underlying. The math of daily reset produces *volatility decay* (β-slippage):

```
Compounded n-day return ≈ L * R_n − 0.5 * L * (L−1) * σ_n² * n
```

Practical academic findings ([Lin, Lin, Wang, Yeh — SSRN 5421274](https://papers.ssrn.com/sol3/Delivery.cfm/5421274.pdf?abstractid=5421274&mirid=1); [arXiv 2504.20116](https://arxiv.org/html/2504.20116v1); [Lu et al. 2009](https://etfbeacon.com/learn/leveraged-etf-decay)):

- LETFs **track their multiple well intraday and over <1 month**.
- Over **>1 quarter**, divergence from L×underlying becomes material; in choppy/sideways markets, decay can be 5–15%/year.
- In **strong, low-vol trends**, LETFs can *outperform* their static multiple via positive compounding.
- Optimal holding period is essentially **1 day to a few weeks**; institutional treatment is "tactical only".

For our agents: LETFs are a useful **short-term tactical lever** (e.g. 5-day momentum trade in TQQQ) but a **bad strategic core hold** (3-month TQQQ position will likely underperform 3x QQQ exposure via futures/options).

### 3.4 Options as embedded leverage

Options give natural leverage with various risk profiles:

- **ATM call**: ~5–10x delta-equivalent leverage on a small premium; **defined risk = premium paid**, but 100% loss is realistic (theta).
- **LEAPS**: long-dated (>1y) deep ITM calls behave like levered stock with ~80–90 delta; LEAPS roll provides quasi-permanent levered exposure with defined max loss.
- **Vertical debit spread**: defined risk *and* defined reward; capital-efficient and the cleanest LLM-friendly leverage instrument.
- **Synthetic stock** (long call + short put, same strike/expiry): replicates 100-share long with much less capital outlay, but the short put creates **undefined downside** (the dangerous part).
- **Naked short calls/puts**: theoretically unlimited loss (calls) or stock-going-to-zero loss (puts). Margin requirement is high but slippage in tail events is unlimited.

[Tastytrade and Britannica](https://www.britannica.com/money/vertical-spread-call-options) and the practitioner literature converge on a clear hierarchy: **defined-risk option strategies are appropriate retail/agent leverage; naked short options are not**.

Alpaca supports options trading and multi-leg orders (per project research note 11 and Alpaca's options docs).

### 3.5 Futures

Not on Alpaca. Skip.

### 3.6 Crypto leverage

Alpaca crypto does **not** offer margin or perps. Skip.

### 3.7 Box spreads as financing

Institutional trick: a long box spread on SPX (e.g. buy 4000C/sell 4500C + sell 4000P/buy 4500P) is a synthetic zero-coupon bond. Selling a box is borrowing at near-Treasury rates (typically T-bill + 0.3–0.5%), much cheaper than broker margin (often T-bill + 1–2%).

Per [EarlyRetirementNow](https://earlyretirementnow.com/2021/12/09/low-cost-leverage-box-spread/), [Alpha Architect](https://alphaarchitect.com/short-box-spreads/), and [Bogleheads](https://www.bogleheads.org/forum/viewtopic.php?t=344667&start=50): box spreads on SPX (European-style, cash-settled, no early-exercise risk) are the standard. Minimum efficient size is ~$100K (the "1000/2000 box"), well above our scale.

**Skip for our system** — overkill at $3K, and Alpaca's options support for SPX index options is limited. Mentioned because you asked.

---

## Section 4 — How autonomous LLM agents should THINK about leverage

This is the section that translates institutional doctrine into prompt-and-Python design.

### 4.1 Hard-coded in Python vs LLM-discretionary

The bright line: **leverage rules are hard-coded; trade selection is discretionary.**

Hard-coded (Python guardrails — agents cannot violate):
- Per-agent max gross leverage cap (e.g. 1.0x → 2.0x scaled by `MASTER_CAPABILITY`).
- Per-position max weight as % of sub-portfolio.
- Drawdown ladder that mechanically reduces effective leverage cap.
- VIX regime gate (lever down outside the sweet spot).
- Forbidden instruments (naked short options, single-stock 3x ETFs held >5 days).
- Daily loss limit that liquidates to cash.

LLM-discretionary (within the cap):
- Position sizing inside the per-position max.
- Long vs cash decision per ticker.
- Use of LETFs vs underlying for a given thesis (subject to holding-period rule).
- Use of defined-risk option spreads vs equity for a given conviction.

This split is the most important design principle. The TradingAgents and TradeTrap academic work ([arXiv 2412.20138](https://arxiv.org/abs/2412.20138); [arXiv 2512.02261](https://arxiv.org/html/2512.02261v1); [arXiv 2512.10971](https://arxiv.org/abs/2512.10971)) shows autonomous LLM agents are *unstable under adversarial conditions* — when the news/data feed is corrupted or extreme, agents accumulate leverage, concentrate positions, and blow up. **Rigid Python caps are the antidote. The LLM never gets the chance to lever beyond the cap, no matter how confident it sounds.**

### 4.2 Conviction × leverage interaction

Strong opinion, evidence-supported: **discretionary leverage based on LLM confidence is dangerous**. LLMs are well-known to be overconfident, miscalibrated, and prone to "agentic momentum" (continuing to rationalize a thesis after evidence has turned). Letting the LLM pick its own leverage is the single fastest path to ruin.

**Rules-based leverage on observable conditions is sound**: VIX regime, realized vol, drawdown state, time since last regime change. These are *exogenous* to the LLM — it can't talk itself into them.

In practice: **conviction maps to position weight within the sleeve, not to leverage of the sleeve.** A high-conviction Opus pick gets 15% of Opus's $1K (vs 5% for low conviction); the *sleeve's leverage cap* is set by Python rules, not by Opus's conviction.

### 4.3 Volatility regime gating

The volatility paradox argues for a "sweet spot" model rather than a monotonic VIX rule:

| VIX | Leverage scalar | Rationale |
|-----|---------|-----------|
| <12 | 0.6× cap | Volatility paradox; vol products and crowded carry stretched |
| 12–18 | 1.0× cap | Sweet spot — strategies work, risk is priced reasonably |
| 18–25 | 0.8× cap | Elevated; trim aggression |
| 25–35 | 0.5× cap | Stress regime; reduce gross |
| >35 | 0.25× cap | Crisis; minimum exposure |

Sources support this non-monotonic shape: the [VIX-managed portfolios paper](https://www.sciencedirect.com/science/article/abs/pii/S1057521924002850) (Moreira & Muir-style scaling) shows monthly leverage adjustment by lagged VIX improves Sharpe, but the academic prescription of "leverage when low VIX" must be **moderated** by the volatility-paradox literature. Hence the haircut at very-low VIX too.

### 4.4 Drawdown-responsive leverage (CPPI-style)

A discrete ladder (operating on rolling-30-day max drawdown of each sub-portfolio):

| Sub-portfolio drawdown | Leverage scalar |
|---|---|
| < 5% | 1.0× |
| 5–10% | 0.75× |
| 10–15% | 0.50× |
| 15–25% | 0.25× |
| > 25% | 0.0× (forced to cash) |

After recovery, the scalar **ratchets back up only after** the portfolio re-enters the prior bucket *and stays there for 5 trading days*. This avoids whipsaw (lever up at the bottom, lever down again immediately).

This is the single most important guardrail. It both protects capital and enforces a healthy *anti-doubling-down* discipline that LLMs are bad at on their own.

### 4.5 Per-strategy leverage caps

Trend/momentum strategies (Haiku) tolerate more leverage:
- Sharpe scales with leverage (well-established CTA result).
- Drawdowns are large but recover; positive convexity in tails.
- Cap: up to 2.0× gross at full `MASTER_CAPABILITY`.

Multi-factor / quality / GARP (Sonnet, Opus): less leverage:
- Concentrated bets carry idiosyncratic risk that doesn't diversify with leverage.
- A single thesis-blow-up at 2x gross is twice the loss but the *downside path* is fat-tailed.
- Cap: 1.25–1.5× gross at full `MASTER_CAPABILITY` for Sonnet; 1.0–1.25× for Opus (concentration discount).

### 4.6 Net vs gross leverage

For a long-only book (our v1), gross = net, so the distinction doesn't matter. Mentioned because *if* we ever extend to L/S, gross becomes the binding constraint (it determines slippage, financing cost, and tail correlation exposure) while net determines beta. Multi-manager pods exploit this — high gross, low net — but it requires real diversification.

### 4.7 Common autonomous-agent failure modes with leverage

1. **Revenge leverage / doubling down**: agent's thesis goes against it; LLM rationalizes "the market is wrong" and increases position into the loss. Caught by: drawdown ladder + per-position cap.
2. **Tail correlation underestimation**: in crisis, all longs go to 1.0 correlation. The agent sees "diversified" 10-stock book; in March 2020 it behaves like a single bet at 10× leverage of any one name. Caught by: VIX gate + portfolio-level vol target.
3. **Procyclical sizing**: strategy worked → agent (or vol-target) sizes up → strategy stops working at peak exposure. Caught by: smoothing the vol estimate (longer EWMA half-life), and the volatility-paradox haircut at very-low VIX.
4. **Frictions amplification**: 2x leverage = 2x slippage, 2x commissions, 2x financing. For a $3K account, this is real — at 2x with weekly turnover, frictions can eat 1–2% of portfolio value annually. Caught by: budget tracking and a "frictions ledger" in the Manager journal.
5. **LETF holding-period drift**: agent buys TQQQ as a "short-term momentum trade", gets distracted, holds 3 months through chop, decays 8%. Caught by: hard 5-day max-hold rule for LETFs, enforced by Python.
6. **Adversarial / corrupted data**: per [TradeTrap](https://arxiv.org/html/2512.02261v1), LLM agents under adversarial inputs make grotesquely levered, concentrated bets. Caught by: hard caps, period.

---

## Section 5 — Concrete recommendations for THIS project

### 5.1 What `MASTER_CAPABILITY` should mathematically do

**Recommendation: `MASTER_CAPABILITY` is a multiplier on the per-agent leverage cap and on the portfolio vol target, applied jointly.**

Concretely:
```python
# Per agent
effective_max_gross = base_max_gross[agent] * MASTER_CAPABILITY
effective_vol_target = base_vol_target[agent] * MASTER_CAPABILITY

# Then apply regime + drawdown scalars on top:
final_cap = effective_max_gross * vix_scalar * dd_scalar
```

Why this design:
- A flat multiplier on weights (option A) doesn't account for vol regime — at low vol, "weights × 1.5" gives much less risk than at high vol.
- A pure vol-target multiplier (option B) is cleaner conceptually but interacts badly with cash-as-default (the agent has to *find* something to lever into).
- A per-agent cap multiplier (option C) is clean and observable but doesn't expose vol-target intuition to the LLM.
- **The hybrid** (cap + vol target moving together) is what institutional risk-parity and CTA shops actually do, and it makes the dashboard slider intuitive: `0.5` = "half-risk mode", `1.0` = "normal", `1.5` = "aggressive overrange".

`MASTER_CAPABILITY` should be **clipped to [0.0, 1.5]** in v1. >1.5 is a footgun; >2.0 should require a separate `OVERRIDE_KEY` env var.

### 5.2 Per-agent leverage caps

Concrete numbers at `MASTER_CAPABILITY = 1.0`, before VIX/DD scalars:

| Agent | Strategy | base_max_gross | base_vol_target | Reasoning |
|---|---|---|---|---|
| **Haiku** | Trend/momentum, 5–20 names | 1.50× | 14% | Trend Sharpe scales with leverage; diversified |
| **Sonnet** | Multi-factor, 8–15 names | 1.25× | 12% | Quality-tilted, less convex; some concentration |
| **Opus** | GARP / concentrated, 5–8 names | 1.00× | 11% | Concentration risk dominates; no leverage premium |

At `MASTER_CAPABILITY = 0.5`: caps become 0.75x / 0.625x / 0.5x — effectively long-only with a cash buffer.
At `MASTER_CAPABILITY = 1.5`: caps become 2.25x / 1.875x / 1.5x — Haiku approaches Reg-T's 2.0x ceiling, which Python should hard-clip.

### 5.3 Volatility-targeting parameters

- **Lookback**: EWMA with **λ = 0.94** (RiskMetrics standard, ~20-day half-life). Cheap, well-studied, stable.
- **Smoothing**: cap *day-over-day change* in target leverage at ±10% to avoid whipsaw on a single noisy day.
- **Floor on realized vol estimate**: `max(realized_vol, 8%)` to prevent absurd lever-up in artificially calm windows (the volatility-paradox fix in the math itself).
- **Cap on implied multiplier**: `min(target_vol/realized_vol, 1.75)` to prevent runaway when vol collapses.
- **Recompute frequency**: nightly, applied at next morning open.

### 5.4 Drawdown-leverage ladder

(Repeated from 4.4, scoped to each sub-portfolio against its own 30-day high.)

| Drawdown | Scalar | Note |
|---|---|---|
| < 5% | 1.00× | Normal |
| 5–10% | 0.75× | Yellow |
| 10–15% | 0.50× | Orange |
| 15–25% | 0.25× | Red |
| > 25% | 0.00× | Forced cash; Manager review required to re-enable |

**Recovery rule**: re-enter prior bucket only after the portfolio sits inside the better bucket for 5 consecutive trading days.

### 5.5 VIX-regime ladder

| VIX (close) | Scalar | Rationale |
|---|---|---|
| < 12 | 0.6× | Volatility paradox haircut |
| 12–18 | 1.0× | Sweet spot |
| 18–25 | 0.8× | Trim |
| 25–35 | 0.5× | Stress |
| > 35 | 0.25× | Crisis |

Apply after drawdown scalar. Both are multiplicative on `effective_max_gross`.

### 5.6 Leveraged ETF policy

**Recommendation: ALLOW for tactical short-term (≤5 trading-day max hold), BAN for strategic positions.** Defended:

- For: LETFs are the cleanest non-options leverage available, and the academic literature ([Lu et al. 2009](https://etfbeacon.com/learn/leveraged-etf-decay)) is clear they track their multiple well over <1 month.
- Against blanket ban: it forecloses a legitimate tool that AQR-style trend strategies use frequently.
- Against unrestricted use: decay over multi-month holds is real and procyclical.

**Implementation**: Python tracks the `entry_date` of every LETF position; an open LETF position older than 5 trading days is **automatically liquidated** at next open. The agent prompts must mention this rule explicitly so the LLM doesn't plan around it.

LETF whitelist: TQQQ, SQQQ, UPRO, SPXU, SOXL, SOXS, TMF, TMV. **Single-stock 2x/3x ETFs banned** outright (e.g. TSLL, NVDL — too much idiosyncratic gap risk).

### 5.7 Options-as-leverage policy

**Recommendation: ALLOW only defined-risk strategies in v1, BAN naked options including naked long calls/puts.** Defended:

- Naked long calls/puts are technically defined-risk (premium = max loss) but in practice agents will repeatedly buy ATM calls, lose 100%, and treat it as a "small position" in their narrative.
- Defined-risk multi-leg structures (debit/credit spreads, iron condors, covered calls, cash-secured puts) require *thinking about both legs*, which forces a more disciplined sizing decision.

V1 whitelist:
- Long debit verticals (call/put spreads).
- Short credit verticals (with both legs defined).
- Iron condors / iron butterflies.
- Covered calls (against existing equity).
- Cash-secured puts (with cash held separately).

V1 blacklist:
- Naked long calls/puts (yes, even though risk is defined).
- Naked short calls/puts.
- Synthetic long stock (long call + short put).
- Calendar/diagonal spreads (Greek complexity exceeds LLM's reliable reasoning).

Per-agent options budget: **max 20% of sub-portfolio in defined-risk options at any time**. Options use counts toward gross leverage at notional delta exposure, not premium paid.

### 5.8 Per-agent leverage paragraphs for the prompts

**For Haiku (trend/momentum):**
> You manage a trend-following sleeve. Trend strategies have a unique property: their Sharpe ratio scales linearly with leverage, so modest leverage is rational when conviction is broad-based. Your maximum gross leverage is set by Python and is currently `{effective_max_gross:.2f}x`. Within that cap, size positions inversely to their recent 20-day realized volatility — high-vol names get smaller weights so each holding contributes roughly equal risk. Never increase a position into a loss ("revenge leverage"). When you see the daily VIX above 25 or below 12, the system has already cut your cap; do not try to compensate by adding higher-volatility names. Leveraged ETFs (TQQQ, SOXL, TMF) are permitted as tactical 1–5 day momentum vehicles only — Python will auto-liquidate any LETF held past 5 trading days.

**For Sonnet (multi-factor):**
> You manage a multi-factor sleeve emphasizing quality and value. Your maximum gross leverage is `{effective_max_gross:.2f}x`. Multi-factor portfolios benefit less from leverage than trend strategies because factor returns are mean-reverting and crowded; the marginal Sharpe gain from leverage is smaller. Use leverage primarily to express *higher-conviction* factor tilts, not to add unrelated names. Defined-risk option spreads (vertical debit/credit, iron condors) are permitted up to 20% of the sleeve as efficient ways to express directional or volatility views. Naked options of any kind are forbidden by Python. When portfolio drawdown exceeds 5%, the system halves your effective cap; do not interpret the cut as a signal to find different ideas — interpret it as a signal to reduce overall exposure.

**For Opus (GARP / concentrated):**
> You manage a concentrated, fundamentals-driven sleeve of 5–8 names. Your maximum gross leverage is `{effective_max_gross:.2f}x` — the lowest of the three sleeves, because concentration carries idiosyncratic risk that does not diversify away with leverage. A single thesis blow-up at 1.5× gross is more punishing than at 1.0× by more than the leverage ratio because position-specific gap risk is non-linear. Prefer to express conviction by concentrating *within* the cap rather than by approaching the cap. Defined-risk option spreads up to 20% of the sleeve are permitted for hedging or efficient expression; LEAPS are particularly attractive for long-duration thesis trades, executed as debit verticals to define risk. Naked options forbidden. When you have a strong thesis, write it down (in the journal) before sizing; if you cannot articulate the disconfirming evidence you'd need to see, halve the size you were considering.

**For Manager (CIO):**
> You allocate the master capability slider and rebalance capital across Haiku, Sonnet, Opus weekly. Default `MASTER_CAPABILITY = 1.0`. Cut to 0.75 when any sub-portfolio is in the 5–10% drawdown bucket; cut to 0.5 when any sub-portfolio is in 10%+. Raise toward 1.25 only after the system has run for at least 6 weeks with realized Sharpe above 0.8 and max drawdown below 7%. Never set above 1.5 without an explicit human override. Reallocate capital toward sleeves with higher *risk-adjusted* return (rolling 30-day Sharpe), not absolute return — leverage already amplifies absolute returns, and you should reward the sleeve with the best signal-to-noise.

### 5.9 Tracking & logging

Dashboard tiles:
- Current `MASTER_CAPABILITY` value and timestamp of last change.
- Per-agent: `effective_max_gross`, current realized gross, current realized 20-day vol, current 30-day max drawdown, current dd-bucket, current VIX-bucket scalar.
- Portfolio-level: realized vs target vol (rolling), gross + net leverage, cash %, % of book in LETFs, % in options.
- Friction ledger: cumulative slippage + commissions + simulated borrow cost as % of NAV.
- A single "Leverage Budget Used" gauge per agent: `realized_gross / effective_max_gross`.

Manager weekly journal entries:
- "Leverage events" log: every regime/dd-bucket change, every cap breach attempt that Python rejected, every LETF auto-liquidation.
- One paragraph: "did leverage help or hurt this week?" — decompose return into beta, alpha, and leverage-amplification.
- Top three leverage decisions of the week with retrospective grade.

### 5.10 Honest pre-mortem

What is most likely to go wrong with leverage in this specific system, and the guardrails that catch each:

1. **Failure**: An agent (probably Opus, given prompt latitude) becomes infatuated with one name, repeatedly tops it up after losses, and the position grows to 30%+ of its sleeve. Combined with 1.0–1.5× gross, a 25% drawdown in that name = ~10–13% of total portfolio gone in a day.
   **Catch**: Per-position max weight (Python-enforced, e.g. 15% of sleeve), drawdown ladder, VIX gate.

2. **Failure**: A long quiet stretch (low VIX, low realized vol) leads the vol-targeting math to push leverage close to cap, and a vol shock (mini-Volmageddon) hits with everything sized up. Multi-day 15–20% loss across all sleeves simultaneously.
   **Catch**: Volatility-paradox haircut at low VIX, floor on realized vol estimate, cap on implied multiplier (1.75×), correlated-drawdown system halt.

3. **Failure**: An LLM agent rationalizes around the rules — e.g. interprets "5-day LETF max hold" as "rotate TQQQ → UPRO → TQQQ every 5 days" to maintain effective long exposure. Mechanically obeys the rule, defeats its purpose, and racks up commissions and tax-equivalent friction.
   **Catch**: Rotation-detection rule (Python flags >2 reopens of same exposure within 15 trading days for Manager review); friction ledger surfaces the cost.

4. **Failure**: Drawdown ladder fires at the absolute bottom (e.g. forced to cash at −25% on a day that turns out to be the local low), then the recovery rule keeps leverage low for 5 days while the market V-shapes, locking in path-dependent loss.
   **Catch**: Accept this — it's the cost of the insurance. The alternative (no ladder) blows up 1-in-N years. Document in the Manager journal so it's not relitigated.

5. **Failure**: Adversarial or hallucinated news input causes one agent to buy 3× a single LETF in one session, brushing the cap.
   **Catch**: Per-order Python check (cannot exceed cap *after* the order), per-day order count cap (e.g. ≤8 trades/day per agent), Manager veto on any single order >5% of sub-portfolio.

6. **Failure**: Slippage and commissions on a $3K account at 2× turnover destroy the "beat SPY net of costs" mandate even when gross alpha is positive.
   **Catch**: Friction ledger as a Manager weekly KPI; if frictions > 50bps/month, Manager mandated to cut MASTER_CAPABILITY by 25%.

7. **Failure**: PDT rule trips on paper account, blocking a legitimate intraday close, leaving a leveraged position open overnight that the agent intended to close.
   **Catch**: Day-trade counter visible to all agents in their context; Python pre-trade check that rejects orders that would force a 4th day-trade in the window.

---

## Executive summary for Brooks

Your leverage system should be **boringly mechanical at the Python layer and intelligently expressive at the LLM layer** — the inverse of the temptation. Hard-code the cap, the drawdown ladder, the VIX gate, the LETF holding period, and the options whitelist; let the LLMs choose what to do *within* those rails. Every credible institutional framework — AQR's vol targeting, Bridgewater's risk parity (warts and all from 2022), CTA Sharpe-scaling, Kelly-with-drawdown-constraints, CPPI — converges on this design. The most dangerous failure mode for autonomous LLM trading is letting model confidence drive leverage; recent academic work on LLM trading agents (TradingAgents, TradeTrap) confirms this empirically. `MASTER_CAPABILITY` should be a joint multiplier on per-agent leverage cap and vol target, clipped to [0.0, 1.5], with regime and drawdown scalars applied on top. Cap Haiku at 1.5× (trend tolerates leverage), Sonnet at 1.25× (multi-factor diminishing returns), Opus at 1.0× (concentration). Allow LETFs only as 1–5 day tactical tools and only defined-risk options structures — ban naked anything. Log leverage usage relentlessly so you can see whether the slider is actually buying you alpha or just buying you variance. The system that survives a year intact is the one where the worst-case path was bounded by code, not by the model's good judgment.

---

*File: `/Users/brooksmoore/Desktop/Multi_Agent_Asset_Competitive_Bot/research/07_leverage_strategies.md`*
*Sources: cited inline above.*
