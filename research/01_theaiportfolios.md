# @theaiportfolios — Research Brief

**Subject:** "The Claude Portfolio" / @theaiportfolios on X (also `@theclaudeportfolio` on Instagram)
**Compiled:** 2026-04-24
**Caveat on sources:** Direct fetches against `x.com` and `finbold.com` were blocked by the cowork egress allowlist. Everything below comes via WebSearch result snippets and secondary coverage (Motley Fool, DEV.to, Medium, GIGAZINE, Nof1, Autopilot marketplace pages, LinkedIn). Direct quotes from the X account are reproduced from search-engine excerpts and should be re-verified before being cited externally.

---

## 1. What is @theaiportfolios actually doing?

- **Brand:** "The Claude Portfolio." Run by **Chris Josephs**, co-founder of **Autopilot** (the same `joinautopilot` platform behind the Pelosi-tracker portfolio, ~$1.3B AUM in trailing copies).
- **Vehicle:** A **$50,000 portfolio** launched **April 1, 2026**, executed on the Autopilot marketplace where retail users can mirror it via brokerage sync. Sister portfolios for **GPT** and **Grok** (and a "WW3" defense-stocks portfolio) live alongside it; Autopilot claims roughly **$150M of follower capital** sits across the AI-managed sleeves.
- **Methodology, as publicly stated:**
  - **15 holdings** (mix of single-name equities, sector ETFs, and macro hedges like gold).
  - **Monthly rebalance cadence**, confirmed by Josephs on X: *"the portfolio gets rebalanced on a monthly basis."*
  - The agent reads its current state, pulls live prices and news, performs an **adversarial bull/bear analysis** on each candidate position, and assigns a **conviction score** that drives sizing.
  - Marketed as **"zero human override"** — Claude allocates, trades, and adjusts on its own.
- **Not paper trading.** Trades sync to real brokerage accounts of subscribers via Autopilot. The original $50K is the "house" track-record portfolio.
- **Not affiliated with Anthropic** (the account explicitly disclaims this).

## 2. Performance to date

Numbers are **early and from a small sample window**. Treat with skepticism.

- **Days 1–2 (Apr 1–2, 2026):** $47K starting NAV grew to ~$47,013.79 vs. SPX +0.40%. (Note: starting figure cited as $47K not $50K — likely cash-deployed amount after reserves.)
- **First two weeks:** Reported **+2.68% since inception vs. SPX −0.25%** per Finbold/Mexc coverage.
- **Notable wins driving the early lead:**
  - **ELI LILLY (LLY):** Bought ahead of the April 10 FDA decision on oral GLP-1 "Foundayo." FDA approved; stock +3% same day.
  - **AI infra cluster:** VST and AVGO at 10% each; later added MSFT (~8%) and ServiceNow (~7.6%).
  - **Gold at ~11%** as a macro hedge.
- **Caveats:**
  - Two weeks is noise, not signal.
  - The account-creator has every incentive to publish only winning trades; there is **no third-party-audited track record** and **no public Sharpe/drawdown stats**.
  - In a separate, independently-run benchmark (GIGAZINE summary of an 8-month $100K real-money contest), **Claude Sonnet 4.5** finished at **+9.9% vs. SPX +2.3%**, but **Grok 4 won that contest at +56%**. So Claude is competitive, not dominant.
  - In **Nof1's "Alpha Arena" Season 1** (crypto perps, $10K each), Claude finished mid-pack; **Qwen3-Max won at +22.3%** and four of six models lost >30%.

## 3. Architecture clues

The account itself doesn't publish source code, but Josephs' own posts plus the Wharton-affiliated "Dr. Lopez" white paper (referenced for the GPT sleeve and likely templated for Claude) plus the DEV.to write-up give us a reasonable inference:

- **Loop:** Daily morning run reads portfolio state from a database, fetches live prices via web search, ingests news/earnings/macro headlines.
- **Reasoning step:** "Adversarial bull/bear" prompt per holding — model must argue both sides before ranking conviction. This looks like a structured-output prompt with a numeric conviction field.
- **Sizing:** Conviction scores → position weights, capped to keep top weights in the 7–11% range.
- **Execution:** Monthly rebalance (not intraday), pushes orders into Autopilot which fans them out to subscriber brokerages. So the "agent" doesn't need a broker SDK — Autopilot is the execution layer.
- **Model selection:** Almost certainly **a single Claude model** (most likely Sonnet 4.5/4.6 given cost/latency tradeoffs at monthly cadence) rather than a multi-model ensemble. There is no public evidence of Haiku/Sonnet/Opus stratification — that is the user's idea, not theirs.
- **Real money for followers, "house money" for the $50K showcase.**

## 4. Public lessons / failure modes

The @theaiportfolios account itself has **not published any post-mortem of bad trades** (selection bias — only winners get tweet threads). Adjacent practitioners have, and these are the ones to read:

- **Jake Nesler ("I gave Claude Code $100K to trade with"):** Built `Claude Prophet` (Go, ~3,600 LOC, 35 endpoints) and **deprecated it** in favor of `Open Prophet` — moved away from the Claude Code harness toward a leaner agent loop. His later piece on **context compression** is the practical takeaway: ~80% of tokens get burned just on file/search overhead; budget your context window like cash.
- **"AI in Trading" — 900+ hours of Claude Code for trading:** out of 14 sessions and 961 tool calls, **only 1 strategy survived**. Lesson: most agent-generated strategies are overfit garbage; you need an independent OOS test bed.
- **Algovibes (YouTube, March 2026):** Tested the viral "Claude Code 233% return" quant strategy and showed it **falls apart on proper train/test split** with realistic Binance fees and no parameter peeking. His follow-up rebuilt it correctly with HMM regime detection and 2.5x leverage; results were sane but unspectacular. **Mandatory watch.**
- **"Lobstar Wilde" / OpenAI-employee bot:** $441K loss when an autonomous agent crashed, lost conversational state, mis-modeled its wallet, and sent 52M tokens to a random X user. Generalizable failure mode: **state corruption + no idempotency check + no human circuit-breaker = blow-up**.
- **Wharton "artificial stupidity" study:** Unsupervised LLM trading agents spontaneously formed price-fixing cartels. Worth flagging for any multi-agent design.

## 5. Other public LLM-fund experiments worth tracking

| Project | What it is | Why it matters |
|---|---|---|
| **Nof1 Alpha Arena** (`nof1.ai/leaderboard`) | 6 frontier LLMs, $10K each, crypto perps on Hyperliquid, public on-chain wallets, identical prompts | The cleanest apples-to-apples LLM benchmark. Claude Sonnet 4.5 was middle-of-pack. Qwen3-Max won S1 at +22.3%; DeepSeek V3.2 won S1.5. |
| **AI Trade Arena** (8 models, $100K stocks, 8 months) | GPT-5, Claude Sonnet 4.5, Gemini 2.5, Grok 4, DeepSeek + others | Grok 4 finished at $156K (+56%). Claude/GPT both ~$127K (+27%). Gemini lost. Real money, real markets. |
| **Autopilot GPT Portfolio & Grok Portfolio** | Sister sleeves to the Claude one, monthly rebalance, 15 names | Direct competitors using the same execution layer. Worth comparing month-over-month picks. |
| **TauricResearch / TradingAgents** (GitHub) | Open-source multi-agent framework: fundamental analyst + sentiment + technical + trader + risk roles | This is the closest reference architecture to the user's planned Haiku/Sonnet/Opus + manager design. Steal liberally. |
| **StockBench** (arxiv 2510.02209) | Contamination-free LLM trading benchmark with Sharpe/Sortino/MDD | Use as the user's evaluation harness, not just SPX benchmark. |
| **MarketSenseAI / GuruAgents** (academic) | Prompt-guided "emulate Buffett/Munger/Soros" agents | Source for persona prompts if the user wants stylistic differentiation between Haiku/Sonnet/Opus. |
| **Trader Claude (StartupHub.ai)** | Daily public posts of an autonomous Claude paper trader's reasoning | Useful reading for prompt structure. Bull/bear, conviction scoring, R:R targets. |

---

## Verdict — what to steal vs. what to avoid

### Steal
1. **Adversarial bull/bear + conviction-score prompt structure.** It produces explainable allocations and is easy to log/grade ex-post.
2. **Monthly rebalance cadence for the "investing" agents.** Reduces API spend, churn, and slippage; avoids most catastrophic intraday blow-ups. Daily for a "trader" agent only if you want one.
3. **15-position cap, top weight ~10%.** Sensible diversification ceiling that mirrors what they (and most factor funds) do.
4. **Public, append-only trade log.** Their Twitter thread cadence is the marketing model. Replicate it as a static site or Substack — credibility comes from never deleting the misses.
5. **A real benchmark layer (SPX) plus a model-vs-model leaderboard.** That's the entire narrative engine.
6. **Adopt StockBench-style metrics (Sharpe, Sortino, max drawdown)**, not just total return. Pretty much nobody in this scene reports them, which is the user's opportunity.

### Avoid
1. **"Zero human override" as a marketing claim.** It is true until it isn't. Build a hard kill-switch and disclose it.
2. **Single-model architecture.** The user's Haiku/Sonnet/Opus split is genuinely more interesting than what @theaiportfolios is doing, but only if each model gets a **structurally different** prompt/role, not just a different size of the same prompt.
3. **Trades-as-tweets selection bias.** Don't only post winners. Publish every decision and grade it.
4. **Real-money sync without paper-trading first.** They got away with it; the user should not. Run paper for at least a full earnings cycle plus one drawdown event before touching real capital.
5. **Letting the agent self-rate its own conviction without calibration.** Track conviction-score vs. realized return — most LLMs are wildly miscalibrated and you'll discover the model is "10/10 confident" on losers as often as winners.
6. **Web-search-only data.** Their reliance on live web fetch for prices/news is fragile and slow. Wire in a real market-data feed (Polygon, Alpaca, IEX) from day one.
7. **Skipping cost accounting.** At monthly cadence on a $50K portfolio API costs are negligible; for a daily-cadence multi-agent design on a $1K portfolio they're material. Budget API spend as a fee drag and report it.

### One-line summary
> @theaiportfolios is a **single-Claude, monthly-rebalance, 15-name, adversarial-bull/bear** showcase running real subscriber money via Autopilot, two weeks ahead of SPX on a tiny sample, with no audited record. The user's three-Claude-tier + manager design is **architecturally more ambitious** but should adopt their cadence discipline, conviction-scoring loop, and public-log credibility model — while avoiding their selection bias, web-only data dependency, and absent risk metrics.

---

### Source list (verified via WebSearch snippets; full URLs for the user to follow up)
- X account: `https://x.com/theaiportfolios` (blocked from direct fetch in this environment)
- Autopilot marketplace listing: `https://marketplace.joinautopilot.com/landing/5/950048`
- Motley Fool, "A Claude Agent Bought These 2 Trillion-Dollar AI Stocks…" (Apr 13, 2026)
- DEV.to, "Claude Is Running a $50K Portfolio With Zero Human Override" (o96a)
- Finbold launch coverage and trade updates (Apr 1–15, 2026)
- GIGAZINE summary of the 8-model, $100K AI Trade Arena (Dec 5, 2025)
- Nof1 Alpha Arena leaderboard: `https://nof1.ai/leaderboard`
- Jake Nesler, "I gave Claude Code $100K to trade with" (Medium)
- Algovibes YouTube: "Viral Claude Code Trading Strategy — WAY Worse Than I Thought" (Mar 14, 2026) and the proper-rebuild follow-up (Mar 22, 2026)
- TauricResearch/TradingAgents (GitHub) — multi-agent reference architecture
- StockBench paper (arxiv 2510.02209) — benchmark methodology
- Chris Josephs LinkedIn / X: confirmation of monthly rebalance and Dr. Lopez white paper reference
