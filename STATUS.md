# Multi_Agent_Asset_Competitive_Bot — STATUS

> Standardized header. Keep these fields at the very top, always current.
> Detailed history lives in `blueprint/` and the `CLAUDE_CODE_*_HANDOFF.md` files. This file is the at-a-glance map.

- **One-liner:** Four Claude models (Haiku/Sonnet/Opus + non-trading Manager) each run a $1K Alpaca paper sleeve; LLMs set target weights, Python sets every dollar and clears every risk check. Goal: beat SPY net of taxes + API costs, ≤ $1/day spend.
- **Stage:** paper-validating
- **Live gate:** OFF (never flipped)
- **Tests:** 83 test files (backtest + temporal walk-forward + duplication parity tests; CL-1 gate in auditor-owned test_audit_sizing_gate.py)
- **Intelligence type:** Full LLM reasoning, heavily fenced (cognitive diversity across model sizes).
- **Single most important next thing:** Run 2-5y real history (using the new temporal walk-forward) for Haiku + Sonnet rules baselines (and Opus stub) and capture excess CAGR + deflated SR vs SPY. Wire the baselines into run_baseline.py for repeatable evidence. (OPEN items from Audit 001 resolved for Fix 1+2.)
- **Honest odds this makes money:** 25–35% to beat SPY over 12 months (per `blueprint/01_HONEST_ASSESSMENT.md`). Worth building as a research instrument regardless.
- **Security posture:** Secrets gitignored (.env/.pem/.key, 2026-06-07). Least-agency is the core strength (LLMs set weights only, Python sets dollars, RiskGate un-bypassable). TODO before live: injection-harden news/EDGAR adapters (CL-5), paper-keys-until-graduation. See `DEFINITION_OF_DONE.md`.
- **Last updated:** 2026-06-09 (Audit 002)

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
- 2026-06-09 (Audit 002): All Audit 001 open items confirmed resolved (Fix 1+2 verified via real git commits; gate file intact). DONE_GROK_HANDOFF_BACKTEST_HARNESS.md renamed per WORKFLOW.md. LEDGER.md Audit 002 appended. Backtest harness thread closed.
