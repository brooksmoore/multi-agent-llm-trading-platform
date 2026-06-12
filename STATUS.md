# Multi_Agent_Asset_Competitive_Bot — STATUS

> Standardized header. Keep these fields at the very top, always current.
> Detailed history lives in `blueprint/` and the `CLAUDE_CODE_*_HANDOFF.md` files. This file is the at-a-glance map.

- **One-liner:** Four Claude models (Haiku/Sonnet/Opus + non-trading Manager) each run a $1K Alpaca paper sleeve; LLMs set target weights, Python sets every dollar and clears every risk check. Goal: beat SPY net of taxes + API costs, ≤ $1/day spend.
- **Stage:** paper-validating
- **Live gate:** OFF (never flipped)
- **Tests:** 756 passing (Robinhood broker 10, log rate-limiter 4, opus book-building 2, dashboard, calibration, lifecycle + all prior suites; CL-1 gate auditor-owned; ultra-review hardening 11 new)
- **Intelligence type:** Full LLM reasoning, heavily fenced (cognitive diversity across model sizes).
- **Single most important next thing:** Run `uv run python scripts/robinhood_oauth.py` once to authorise → then `uv run python app.py` to start dry-run → then fund account 981398050 + flip `ROBINHOOD_LIVE_ENABLED=true`.
- **Honest odds this makes money:** 20–30% to beat SPY over 12 months (per `blueprint/01_HONEST_ASSESSMENT.md`). Worth building as a research instrument regardless.
- **Security posture:** Secrets gitignored (.env/.pem/.key, 2026-06-07). Least-agency is the core strength (LLMs set weights only, Python sets dollars, RiskGate un-bypassable). XML tag spoofing closed (CL-5 ext, 2026-06-12). See `DEFINITION_OF_DONE.md`.
- **Last updated:** 2026-06-12

---

## Stage vocabulary
`idea → skeleton → core-done → runner-wiring → paper-validating → live-gated → live`

## Recent movement
- 2026-06-07: Portfolio review baseline established. Highest ceiling / highest variance / most expensive to run.
- 2026-06-07: Security pass — gitignore hardened (.env/.pem/.key); DEFINITION_OF_DONE.md added. Flagged: news/EDGAR adapters need a prompt-injection handoff (model on hood_agent_1's) before live.
- 2026-06-07: Backtest harness advanced (Sonnet rules baseline with identical 12-1 mom signal math, fail-before TDD tests, deflated SR + walk-forward scaffold in engine, CL-1 "no conviction leaks to sizer" gate). Per STATUS + cross-learning handoff. Tests green on touched modules; no invariants or live gates touched.
- 2026-06-07 (Audit 001): Audit of backtest harness — 5 items verified, 4 open. Key findings: walk-forward is a stub (cost sweep, not temporal split — DoD OPEN-1), _SONNET_TRADABLE duplicated (OPEN-2), fail-before unverifiable without intermediate git commits (OPEN-3). CL-1 gate extracted to auditor-owned `tests/test_audit_sizing_gate.py` (OPEN-4 resolved). LEDGER.md created.
- 2026-06-07 (Fix 1+2): Real temporal walk-forward date splits implemented (RED test c221efb → GREEN 89b0a65). _SONNET_TRADABLE duplication eliminated by direct import from live agent (RED test 564fae2 → GREEN 7b62629). All auditor-specified fail-before + git evidence provided. Status next advanced to real historical runs.
- 2026-06-08: Dashboard fixes: weekends stripped from NAV vs SPY chart, Manager excluded from sleeve equity chart, per-sleeve P&L table moved to bottom. Fixed calibration chart empty bug: CalibrationRecorder was silently failing to record wins/losses because conviction lookup read from OMS Order payload (which doesn't store conviction — it's on Intent). Fixed by adding AgentMemory.conviction_by_intent_id + passing memories to CalibrationRecorder. 740 tests passing.
- 2026-06-09 (Audit 002): All Audit 001 open items confirmed resolved. Backtest harness thread closed (DONE_). Committed all pending working-tree work (Robinhood broker, calibration conviction fix, dashboard UX, opus H1/H2 book-building, log rate limiter) as 3d0c5ca + 8c57dc4. 740/740 green.
- 2026-06-10: CL-5 prompt injection hardening: sanitize_external() strips C0 control chars from all external content (news/EDGAR/RSS) before it enters LLM prompts; <external_content> structural tags wrap all external blocks; explicit injection policy added to all three agent system prompts. 19 new tests, 745/745 green. DoD pre-live security requirement now met.
- 2026-06-10: Robinhood MCP probe complete. Verified all tool names, field shapes, account structure via live list_tools() call. Rewrote execution/robinhood_broker.py: correct tool names (place_equity_order, review_equity_order, cancel_equity_order, get_equity_orders, get_equity_positions, get_accounts), account hardcoded to 981398050, ref_id idempotency key, review_equity_order called before every live placement. All 10 broker tests green. 740/740 passing. Next: fund agentic account + dry-run session.
- 2026-06-12: Robinhood OAuth token provider: replaced static ROBINHOOD_AUTH_TOKEN with OAuth 2.1 PKCE flow. scripts/robinhood_oauth.py registers a client + catches browser redirect, persists to ~/.robinhood_token.json. execution/robinhood_token.TokenProvider auto-refreshes before expiry. RobinhoodBroker now takes token_provider (None = dry-run, no token needed). 756/756 green.
- 2026-06-12: Ultra review hardening (Fable 5, 15 findings, 12 applied). Blocking fixes: concurrent duplicate-submit sentinel (_INFLIGHT), response dict guards in get_order/find_order_by_client_id, nil UUID → random UUID in _to_uuid, zero-price fill guard in LotLedger dust check, _fetch_last_price returns None not Decimal(0), cached_price falsy-zero fixed. Other fixes: daytrade_count null warning, broker_kind validation at startup (ValueError), sleeve allocation Decimal(0) falsy fix, XML tag spoofing closed (sanitize_external escapes < >). Skipped: #8 false positive, #12 intentional os._exit, #15 reconcile script. 11 new tests, 756/756 green.
