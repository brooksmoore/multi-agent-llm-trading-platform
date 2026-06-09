# Honest Assessment — Multi-Agent Asset Competitive Bot

**Date:** 2026-04-24
**Companion to:** `00_BLUEPRINT.md`
**Tone:** Candid. You asked for my analysis on which of your and Gemini's ideas are sound, so I'm giving it straight.

---

## TL;DR

The project is **worth building** and the architecture in `00_BLUEPRINT.md` will work. But the goal needs honest framing:

- **Likelihood the bot meaningfully beats SPY net of taxes and API costs over a 12-month sample:** ~25–35%. That sounds low; it's actually optimistic. The median active human manager fails to beat SPY net of costs over multi-year periods. The Nof1 Alpha Arena and AI Trade Arena experiments cited in research file 01 show LLMs landing all over the leaderboard with high variance — sometimes Grok wins, sometimes Qwen, sometimes Claude. Two weeks of @theaiportfolios outperformance is noise. Eight months of Claude Sonnet 4.5 placing mid-pack against Grok and DeepSeek is a more representative sample.
- **What the project is *probably* worth building anyway:** a real research dashboard, a public-credibility-grade trade journal, and a personal data asset that compounds in usefulness over time even if the bot just matches SPY. The Bloomberg-lite terminal alone is valuable.
- **The single biggest risk:** not the market, not the models, not Alpaca. It's **state corruption + a missing kill switch**. The Lobstar-Wilde $441K blow-up was caused by an autonomous agent that lost its conversational state, mis-modeled its wallet, and kept acting. Our blueprint addresses this with reconciliation every 60s, append-only event log, and a global kill switch — but only if we *actually build them first*, before the LLM agents go live.

---

## What in the Gemini transcript is sound

These ideas survive scrutiny:

1. **Pivoting from "high-frequency scalper" to "team of strategic advisors with longer holding periods."** This was the most important course correction in the entire conversation. LLMs cannot beat HFT at speed; they *can* synthesize daily/weekly information meaningfully.
2. **Heterogeneous models (Haiku/Sonnet/Opus) instead of three Sonnets.** Cognitive diversity > correlated errors. Confirmed in research file 01 by the Wharton "artificial stupidity" finding that homogeneous LLM agents form spontaneous price-fixing cartels — an unsettling failure mode that goes away with model diversity.
3. **Distinct functional mandates per agent (TA / FA / Macro), not the same prompt with different model sizes.** This is what gives the diversity teeth. Three Claude models with the same prompt is just an expensive ensemble of a single forecaster.
4. **A 4th Manager agent that doesn't trade.** Allocator, risk overseer, weekly reporter. Architecturally this is the right shape.
5. **Hard-coded Python guardrails on position size, max bet, kill switch.** Non-negotiable. Gemini was right to insist the LLM never decides max bet size.
6. **Benchmarking against SPY *net of taxes and API costs*.** Most LLM trading projects benchmark gross of costs, which is a vanity metric. Net is the right hurdle.
7. **Paper trading first.** Mandatory. Anyone who skips this is gambling, not researching.
8. **Prompt caching as the budget lever.** 90% off cached input is real and substantial. Sonnet 4.6 and Opus 4.7 with proper caching put $1/day in reach where it absolutely was not in 2024.
9. **Heterogeneous cadence per agent** (Haiku quick, Sonnet medium, Opus deep) → matches budget allocation to model strength.
10. **The "Information Arbitrage" framing for what the bot can plausibly do.** Read a 10-Q faster than a human, react to a Fed pivot before midday, synthesize earnings + macro + price action into a coherent thesis. Realistic LLM job description.

## What in the Gemini transcript is wrong, hand-waved, or dangerous

These I am dropping or modifying:

1. **The "Bayesian Swarm" / Kelly Criterion with LLM-stated confidence as the probability.** This sounds rigorous but is cargo-cult. Kelly requires a *calibrated* probability and a *known* payoff distribution. An LLM's stated 9/10 confidence is not a calibrated probability — research file 01's calibration warning is exactly right. **Use fixed-fractional or vol-targeted sizing. Never let the LLM's stated confidence drive position size.**
2. **"Reallocate capital weekly between agents based on returns."** This chases noise and is the exact bias retail traders get destroyed by. Eight weeks of risk-adjusted return is the *minimum* meaningful sample. The blueprint uses 8-week rolling Sharpe, not weekly P&L.
3. **"Modified Sharpe with tax drag as the agent objective function":** the formula is fine, but having the LLM *optimize* the function inside its prompt is theatre. The Python sizing module enforces this; the LLM doesn't compute it.
4. **"Token Credits" as a motivation/reward system that gates context window size.** Cute analogy, no real-world value, adds complexity without measurable benefit. Skipped. The agents either get the data they need or they don't — gating it as "punishment" makes them dumber, not more motivated.
5. **"Compute Reward / Capital Survival" where the loser's strategy is replaced by the winner's.** This converges the system to a single strategy, defeating the entire diversity argument. Skipped. If an agent loses for 8 weeks, **bench it and reduce its capital** — don't force it to mimic the winner.
6. **The $1.00/day budget at the *original* "high-frequency scalping" cadence.** Mathematically impossible. At a strategic-advisor cadence with monthly rebalance + daily monitoring (the cadence the blueprint adopts), $1.00/day is feasible. Gemini didn't make this distinction clearly.
7. **Robinhood for stock/options automation.** `robin_stocks` is an unofficial scraper that breaks on app updates and is TOS-gray. We're using Alpaca (official, paper-native, free). Robinhood Crypto has a real API but its 24/7 trading is a footgun for a $1K sleeve at 0.25% fees + spread.
8. **"Prediction markets on Robinhood" / Kalshi arbitrage.** Out of scope for v1. Kalshi spreads and contract sizing make small-bankroll arbitrage harder than Gemini implied, and adding a third venue triples the surface area for a system that hasn't yet proven itself on stocks.
9. **"24/7 crypto compounding via Haiku for the weekend gap."** Tempting, but Alpaca crypto is 25 bps per side plus spread, and the volatility punishes naive sizing. Defer until equity discipline is proven.
10. **"Letting agents see each other's portfolios so they can copy/critique."** This is the exact recipe for the cartel problem the Wharton paper identified. Agents in v1 see *only their own state* + the manager's allocation decision. Cross-agent visibility is a v2 experiment, fenced behind explicit research.
11. **"100% annual returns safely."** You already retired this. Good. The fact that you walked it back unprompted is the single best signal in the entire transcript that you'll engage with this project honestly.

## What's missing from the Gemini transcript entirely

These are the most important additions in the blueprint:

1. **A serious backtesting harness.** Gemini barely mentioned it. The blueprint mandates 2–5 years of historical backtest per strategy *before* any agent gets paper capital, plus walk-forward CV and deflated Sharpe to guard against overfitting from parameter sweeps. The Algovibes "viral 233% Claude Code strategy" was overfit garbage on proper train/test split — that's the cautionary tale to internalize.
2. **Calibration tracking.** Conviction-score vs. realized return must be tracked from day 1. If "9/10 conviction" trades aren't right materially more often than "5/10 conviction" trades, the conviction signal is noise and the prompt structure needs to change.
3. **Append-only event log + reconciliation loop.** This is what prevents Lobstar-Wilde-style state-corruption blow-ups. Hard to overstate how important this is.
4. **The "rules-only baseline" sleeve per strategy.** If the LLM-driven version doesn't beat the deterministic version of the same strategy, we drop the LLM and run it deterministically. This is the only honest test of whether the LLM is *adding* value.
5. **A "graduation criteria" gate before any real capital.** The blueprint defines this concretely: 8+ weeks paper, beats SPY net of costs, beats rules-only baseline, calibration passes, weekly journal kept. If we hit those, deploy $500 of real capital. If we don't, the system is a research dashboard, not a fund.
6. **Public-grade trade journal.** Every decision (including rejected) logged in Markdown, no selective publication. This is what gives @theaiportfolios its credibility (despite their selection bias on what they tweet) — and what we should *exceed* by publishing the misses too.
7. **A hard kill switch the user controls from the dashboard.** One button, immediate effect, no agent override possible.

## The risks I'm most worried about

In rough order of probability × severity:

1. **Subtle bug in the OMS state machine** that causes a doubled or zero'd position the first time something unexpected happens at the broker (partial fill at market close, after-hours news, halt-then-reopen). Mitigation: build OMS first with `FakeBroker`, write `test_oms_recovery.py` to specifically simulate crash-mid-trade and partial fills, reconcile every 60s.
2. **API budget overrun** during a volatile day when every agent wants to call Opus simultaneously. Mitigation: hard `daily_spend.json` ceiling that the LLM wrapper enforces; degrade to Haiku-only mode rather than overspend.
3. **Overfit strategies that look great in backtest and lose money live.** This is the single most common failure mode of every retail bot. Mitigation: walk-forward CV, deflated Sharpe, ≤3 tunable parameters per strategy, mandatory rules-only baseline.
4. **The bot beats SPY for 8 weeks by sheer luck** and we promote to real money just before reverting to the mean. Mitigation: 8 weeks is the *minimum*, not the target; require Sharpe > SPY's Sharpe (not just return), require calibration to pass, require beating the rules-only baseline. Multiple gates make luck-driven graduation harder.
5. **Anthropic changes pricing or rate limits** during the project (already happened once in early 2026 with the cache-TTL regression). Mitigation: budget enforcer is parameterized; fall back to cheaper models gracefully; daily P&L report includes API cost so we notice immediately.
6. **The user (you) loses interest before the 8-week paper sample completes** because no live money = no dopamine. Mitigation: the dashboard's daily journal and weekly report should be genuinely fun to read; we publish weekly to a private Substack-style page so the project has a public record. Also why I recommend NOT going live with real money until graduation criteria are met — early real-money trading is the fastest way to lose interest *and* lose money.
7. **A "this is fine" moment** where one agent silently degrades for two weeks and we don't notice because the others are carrying the aggregate. Mitigation: per-agent kill switches at 5 consecutive losing trades; per-agent equity curve front-and-center on the dashboard; weekly report attribution by agent.

## What I'm *not* worried about

- The math of $1/day. It works. It's tight, but it works. Research file 04 verifies it concretely.
- Alpaca breaking. They have outages but not catastrophic ones; reconciliation handles transient issues.
- The agents "hallucinating a stock that doesn't exist." Easy to validate symbols against Alpaca's universe in the RiskGate.
- Token costs eclipsing trading P&L. At $0.60/day = ~$220/yr, the API cost on a $3K notional account is ~7.3% — meaningful but not catastrophic. If the bot can't generate >7.3% alpha over SPY net of taxes, it shouldn't run real money anyway and we'll learn that on paper.

## Bottom line

Build the system in the blueprint. Use the budget. Run paper for 8+ weeks. Read the weekly reports honestly. If the system beats SPY net of costs and beats its own rules-only baseline, deploy a small real-money slice and re-evaluate. If it doesn't, you'll have built one of the more interesting personal research dashboards in the LLM-trading space — and you'll know firsthand, with your own data, exactly where current LLMs stand as portfolio managers. That answer alone is worth the project.

The single most important thing I want you to walk away from this assessment with: **build the boring infrastructure first**. OMS, RiskGate, kill switch, reconciliation, append-only log. Before any agent makes a single decision. That order is what separates this project from the $441K blow-ups.

---

*End of assessment. Open questions for sign-off are in §13 of `00_BLUEPRINT.md`.*

---

## 2026-06-09 — Rules-only baseline backtest results (5-year, walk-forward)

**Run by:** Claude (auditor), standing in for Grok. Data via yfinance, 2021-06-09 → 2026-06-09.
**Purpose:** stake-in-the-ground numbers for the DoD "beats rules-only baseline" gate; to be compared against live LLM sleeve performance as paper trading accumulates.

### Haiku — Faber GTAA (SMA trend-following, ETF universe)

| Metric | Value |
|---|---|
| CAGR | +4.4% |
| SPY CAGR | +11.8% |
| **Excess vs SPY** | **−7.4%/yr** |
| Ann. volatility | 6.8% |
| Sharpe | 0.69 |
| Max drawdown | −9.1% |

**Walk-forward (4 windows):** 1 of 4 beat SPY (+5.2%, −24.8%, −16.9%, −20.3%)

**Honest read:** This is a risk-reduction strategy, not a return-maximizer. In a 5-year bull market, going to cash when assets are below their SMA is costly — but the −9% max drawdown vs SPY's worse 2022 drawdown shows the signal doing what it was designed to do. The bar for the Haiku LLM sleeve is: beat this rules baseline, not beat SPY directly.

### Sonnet — Price momentum (top-5 by 12-1 momentum, 74-name universe)

| Metric | Value |
|---|---|
| CAGR | +18.8% |
| SPY CAGR | +11.8% |
| **Excess vs SPY** | **+7.0%/yr** |
| Ann. volatility | 14.0% |
| Sharpe | 1.32 |
| Max drawdown | −17.4% |

**Walk-forward (4 windows):** 1 of 4 beat SPY (+6.1%, −15.0%, −18.6%, −25.4%)

**Honest read:** The full-period win is real but period-dependent. It was almost entirely driven by the 2021–mid-2022 momentum tailwind (tech mega-caps). Once that reversed, the strategy lagged badly in 3 of 4 subsequent windows. The walk-forward is the more honest picture. The Sonnet LLM sleeve's job is to add judgment on top of this signal — e.g. recognising when the momentum factor is likely to mean-revert and reducing exposure.

### Opus — GARP discretionary

**Not backtestable.** Opus's signal is its fundamental reasoning (EDGAR filings, earnings quality, competitive positioning) — there is no mechanical rule to replay on historical prices. The LLM reasoning *is* the strategy. Live paper trading is the only valid test.

### Implications for honest odds

The 25–35% probability of beating SPY quoted at project start was already conservative. These results suggest the rules-only signals are not reliably positive in the recent 5-year sample, and the walk-forward consistency is poor (1/4 windows for both). The LLM layer needs to add edge on top of marginal baselines AND cover ~$0.50/day API cost.

**Revised framing:** 20–30% probability of beating SPY net of costs over a 12-month live sample. Still worth building — the downside is a research dashboard and personal data asset, not a loss. But the numbers should not be read optimistically.

---

## 2026-06-07 — Security pass addendum (Anthropic agent-security principles)
**What (done now):** `.gitignore` hardened (`*.pem`/`*.key`/`*.p12` added alongside `.env`; no secret in git history). Added `DEFINITION_OF_DONE.md` with a security section.
**Assessment vs the four principles:**
- *Least agency:* ALREADY the system's core strength — LLMs propose weights only, Python computes every dollar, RiskGate is un-bypassable, no cross-agent visibility (cartel guard). This is exactly what the principle asks for.
- *Static keys = compromised:* mitigate via Alpaca PAPER keys until graduation (a leaked paper key moves no real money); at graduation, broker key scoped to trading only, no funding/withdrawal.
- *Sandbox untrusted input:* GAP — four LLM agents ingest EDGAR/Finnhub/RSS text with no prompt-injection defense yet. Needs a `GROK_HANDOFF_SECURITY_INJECTION.md` modeled on hood_agent_1's (delimit + label untrusted source text, fail-safe schema validation) before live.
- *Dynamic per-task scope:* order-capable keys present only during live sessions.
**Status:** secrets ✅; untrusted-input hardening 🔴 OPEN (write the injection handoff for data/news.py + EDGAR adapter before any real capital). Tracked in DEFINITION_OF_DONE.md.
