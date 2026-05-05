# Blueprint Changelog

## v0.4 — 2026-04-25 (professional leverage system + free-data lock + simplified process mgmt)

### Locked-in user decisions
- **No paid data sources.** Earnings call transcripts (paywalled) are out of scope for v1. The free stack (EDGAR + Finnhub free + yfinance + RSS + FRED + CoinGecko + Treasury.gov) is enough to know whether the architecture works. New §17 documents the full source list, including how Opus deep-dives compensate for missing transcripts (post-earnings press coverage as a Q&A proxy).
- **Process management simplified**: foreground `python app.py` in a regular Terminal window, manual start/stop, prevent Mac sleep with `caffeinate -dimsu` or System Settings. Matches user's existing autotrader workflow. No tmux, no launchd in v1.

### New: full leverage system (research file 07)
- **§16 added**: complete professional leverage spec derived from CTA, risk-parity, prop-desk, and LLM-trading-agent literature.
- **`MASTER_CAPABILITY` redesigned** as a *joint multiplier on per-agent leverage cap and vol-target*, not a flat weight scalar. Range expanded to [0.0, 1.5] (default 1.0); >1.5 requires `OVERRIDE_KEY`.
- **Per-agent base leverage caps**: Haiku 1.50× / Sonnet 1.25× / Opus 1.00× — reflecting that trend tolerates leverage, multi-factor diminishes, concentration doesn't diversify.
- **Vol-targeting math** (EWMA λ=0.94, 8% realized-vol floor, 1.75× implied-multiplier cap, ±10% day-over-day change cap).
- **Drawdown-leverage ladder** (per sub-portfolio): <5% 1.00×, 5–10% 0.75×, 10–15% 0.50×, 15–25% 0.25×, >25% 0.00× with 5-day recovery rule.
- **VIX-regime ladder** (the volatility paradox): <12 0.6×, 12–18 1.0×, 18–25 0.8×, 25–35 0.5×, >35 0.25×.
- **Leveraged ETF policy**: allowed for ≤5-day tactical holds (Python auto-liquidates), broad-index whitelist only, single-stock LETFs banned, anti-rotation rule (>2 reopens / 15 days flagged).
- **Options policy**: defined-risk multi-leg only (verticals, condors, covered calls, CSPs); naked anything banned including naked long calls; ≤20% of sub-portfolio in options at notional delta exposure.
- **Manager owns the slider**: mandatory cuts on drawdown buckets; raise toward 1.25 only after 6 weeks of Sharpe > 0.8 and DD < 7%; never above 1.5 without human override.
- **Six-week leverage observability gate** before considering MC > 1.0.
- **Honest pre-mortem**: top 7 leverage failure modes with the specific guardrail that catches each.
- **All four agent prompts updated** with leverage paragraphs tailored to each agent's strategy.
- **Manager prompt** gains a new `mc_proposal.json` call schema and journal sections for leverage retrospective and friction ledger.
- **Dashboard updated** to expose `effective_max_gross`, "Leverage Budget Used" gauges per agent, friction ledger, and the MC slider with current value.

### Section renumbering
Old §15 (Data sources) → §17. New §16 = Leverage system. Old §16 (Reference: agent prompts) → §18.

---

## v0.3 — 2026-04-25 (user feedback round 2 + SDK research)

### Locked-in user decisions
- **Reactive Haiku news scans** are confirmed as the primary use of leftover daily API budget. Trigger: Python detects >2σ move on a held name, >1.5σ on SPY/VIX, or tagged macro event. Haiku scan ($0.02) decides if material; can escalate to Sonnet ($0.06).
- **Master leverage lever** (`MASTER_CAPABILITY` ∈ [0.0, 1.0], default 1.0). Multiplies every intent's target weight before sizing. Dashboard slider. 0.0 = read-only mode (memos still produced). New §1 principle 7 + new RiskGate Layer 5.
- **Telegram is a hard yes** for v1.5 notifications. Adapter stub from milestone 1; full integration after milestone 7.
- **Process management plan**: tmux during build/dev; migrate to launchd user agent (`KeepAlive=true`, log files in `logs/`) when stable.

### Resolved unknowns (research files 05 + 06)
- ✅ **alpaca-py 0.43.2 supports Level-3 multi-leg options as a single call.** `OrderClass.MLEG` + `OptionLegRequest`, hard cap 4 legs, paper auto-Level-3. Bracket/OTO not supported on MLEG (manage stops client-side). `close_position` doesn't unwind atomically (per-leg unwind).
- ✅ **Anthropic Batch API supports tool use, prompt caching (stacks with 50% batch discount), extended thinking.** Workspace-level cache isolation since 2026-02-05 — keep batch + live in same workspace. Extended-output beta (300K max_tokens) is batch-only on Opus 4.6/4.7 and Sonnet 4.6.
- ✅ **vectorbt is VIABLE** for the backtest harness. Proxy benchmark in Linux container shows 24×100 walk-forward sweep in ~4.5s; on M2/M3 with real vectorbt expect 3–10s wall-clock, RAM <500MB. Bottleneck will be data ingestion + LLM steps, not vectorbt.

### New / clarified
- **Free-tier vs paid data sources clarified** in §8 and new §15. Anthropic free-tier was a non-issue (you pay per token); the "free tier" hedge was about *data sources* (Finnhub free, NewsAPI free, EDGAR/RSS always-free). Paid Finnhub ($35/mo) flagged as recommended v1.5 upgrade — unlocks earnings call transcripts, Opus deep-dives' biggest information gap.
- **Agent prompts v1** drafted in `prompts/`. Strict JSON outputs (Manager weekly journal is markdown). Designed to be cached as 1h-TTL prefix; per-call user messages fill `{{double-brace}}` variables. Hard rules in prompts are advisory; RiskGate enforces.
- **Dashboard top strip** now includes the MASTER_CAPABILITY slider as a top-level control.
- **News + reactive section** added to §2 cadence table.

### Open items still flagged
- Whether 200K-token Opus deep-dive is actually better than 80K (will measure by week 3).
- Whether to upgrade to Finnhub paid for transcripts (deferred to post-milestone-7).

---

## v0.2 — 2026-04-25 (incorporates user feedback round 1)

### Locked-in user decisions
- **Full autonomy from day 1** in paper. Approval queue stays as a code path behind `AUTO_APPROVE` feature flag (default true in dev). Built as if real money.
- **Universe expanded** to all liquid markets practical via Alpaca: US equities, US-listed ETFs (incl. international/EM/commodities/bonds via ETF), Alpaca crypto (BTC/ETH/SOL focus), and Level 3 options (paper auto-approved).
- **Manager re-allocation cadence:** 4 weeks (down from 8), based on rolling 4-week Sortino. Capped at ±25% per move.
- **Crypto on Haiku:** approved despite 0.25%/side fee. ~30% of Haiku's $1K, BTC/ETH/SOL.
- **Privacy:** local-only. No Substack. Telegram alert adapter stubbed for later.
- **Real-money graduation gate:** 6 weeks (down from 8, up from user's 4). Statistical compromise.
- **Tax bracket default:** 30% short / 15% long, configurable. "Generally tax-aware" without claiming to be a tax advisor.
- **Opus cadence:** Option C — daily lightweight cached read (~$0.03) + two scheduled deep-dives per week (Thursday + Friday, ~$0.40 each, ~150–200K-token context). Adaptive trigger router deferred to week 3 if scheduled cadence proves wasteful.

### Changes from v0.1
- **Drawdown ladder relaxed** for paper-trading data collection: -15% halve / -25% pause / -33% liquidate (was -10/-15/-20).
- **Kept** -2% intraday loss limit as a "bug-detection" trip wire.
- **Manager mandate expanded** from 4 jobs to 6: added (5) weekly regime read injected into shared cached context, (6) adversarial critique of each agent's highest-conviction weekly intent.
- **Backtest harness simplified.** Dropped the custom event-driven LLM-replay sim. We now do rules-only backtests in vectorbt and run rules-only baseline *in parallel* with LLM sleeve in paper for the A/B. Cuts ~800 LOC and is more honest.
- **Real-money graduation criteria revised**: LLM sleeve must beat rules-only baseline on **max drawdown and drawdown duration**, not necessarily total return. Honest test of where LLMs add value.
- **Tax-aware section added** as its own §6: lot-level accounting from day 1, wash-sale checker in RiskGate, long-term-gains preference flag, weekly tax-loss harvesting in Manager job, year-end optimization sweep, dual gross/net leaderboard.
- **Lot-level accounting** added to OMS (new `lots.py` module + `lots` SQLite table + FIFO/LIFO consume). Required for tax-aware behavior.
- **Haiku dual-mandate**: 70% ETF GTAA (Mon–Fri) + 30% crypto trend (24/7). Crypto sleeve sized so a bad week ≤3% drag on aggregate.
- **Opus daily $0.03 prior-memo cached read** added so Opus has continuous awareness without expensive daily reasoning.
- **LOC target updated** from 3,000 to 5,000 (incl. tests + tax/lot modules + prompts). The software-team lens is right that 3K was optimistic.
- **Dashboard layout revised** from 4-pane grid to per-agent columns. Easier to compare agents side-by-side and matches "Claude analyzes performance with you" use case.
- **Anthropic 529 retry/backoff** added to LLM wrapper requirements.
- **Section §14 added**: "Things I don't know" — flagged unknowns I'm carrying forward as open items rather than guessing.

### Lens-review notes (from honest-assessment file)
- **Investment bank lens:** approves strategy mix (multi-factor + GARP + GTAA + risk overlay = institutional-orthodox); would push for international + fixed-income beyond TLT/IEF + paid data feeds (deferred — budget).
- **Software-team lens:** approves architecture; pushed back on 3K LOC (now 5K); flags lack of CI/CD and deployment story (defaulting to tmux; revisit).
- **Mathematician lens:** approves deflated Sharpe + walk-forward CV + rules-only baseline; flags 4-week re-eval as statistically thin (acknowledged); would want Bayesian posterior on "true edge exists" updated each week (added as nice-to-have for the Manager's weekly journal).

### Open items requiring later answers
- Foreground tmux vs. launchd for process management.
- Whether 200K-token Opus context is actually better than 80K (will measure).
- Whether `vectorbt` performance is acceptable on user's Mac (will benchmark at milestone 5).
- Whether Batch API supports tool use in April 2026 (will discover at milestone 7).
- Whether earnings call transcripts are free-tier accessible (will know after first Opus deep-dive).

## v0.1 — 2026-04-24 (initial)
- Initial blueprint after deep research across 4 research files.
- Original drawdown ladder: -10/-15/-20.
- Original re-eval cadence: 8 weeks.
- Original Manager mandate: 4 jobs.
- Original Opus cadence: 1 morning call + 1 EOD batched review.
- Original real-money graduation: 8 weeks.
- Original LOC target: 3,000.
