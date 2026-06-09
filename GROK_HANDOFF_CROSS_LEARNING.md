# GROK HANDOFF — Cross-Agent Learnings (Multi_Agent_Asset_Competitive_Bot)

**Date:** 2026-06-07 · Auditor (Claude) → Builder (Grok). Source: portfolio review §5 + `../WORKFLOW.md`.
Improvements this agent should adopt from the rest of the portfolio. Scoped passes, fail-before tests. Do NOT touch the RiskGate / OMS invariants.

## CL-1 — LLM is weight-only, never sizes on its own confidence (HIGH PRIORITY — your own assessment demands it)
**Why:** `blueprint/01_HONEST_ASSESSMENT.md` already says it: drop Kelly-on-LLM-confidence; LLM-stated 9/10 is not a calibrated probability. kalshi_1.0 died proving this. The architecture says "LLMs propose weights, Python computes dollars" — audit that this holds with ZERO leakage.
**Adopt/verify:** no code path lets an LLM confidence/conviction number scale a position. Sizing is fixed-fractional or vol-targeted in Python. Add an audit-gate test that fails if an LLM scalar reaches the sizer.

## CL-2 — Adopt truleo's auditor-owned gate tests (PORTFOLIO STANDARD)
**Why:** with 81 test files, theater risk is real — many tests, but which ones actually defend the invariants? truleo's pattern: dedicated `test_audit_*_gate.py` owned by the auditor, builder may NOT edit.
**Adopt:** create auditor-owned gates for the non-negotiables: RiskGate cannot be bypassed, budget breaker holds ≤$1/day, reconciliation flips to RECONCILIATION_BREAK on mismatch, no cross-agent visibility. Grok must not modify these files.
**Reference:** `../truleo_agent/tests/test_audit_*_gate.py`.

## CL-3 — Standing HONEST_ASSESSMENT.md (YOU ARE THE TEMPLATE)
**Why:** you have the best one in the portfolio (`blueprint/01_HONEST_ASSESSMENT.md`). Keep it current — update the "probability this beats SPY" and risk list as paper data comes in. Other agents are copying this format from you.

## CL-4 — Shared calibration tracking (HIGH PRIORITY)
**Why:** the assessment mandates calibration from day 1 — does 9/10 conviction beat 5/10? If not, the conviction signal is noise and the prompt structure must change. This is the metric that would have caught kalshi's anti-calibration in week one.
**Adopt:** first-class, queryable conviction-vs-realized log per agent sleeve, feeding the SCOREBOARD.md panel. Make it impossible to ship a sleeve to real capital without this series existing.

## CL-5 — Security: injection-harden the news/EDGAR/RSS adapters (from hood's pass)
**Why:** four LLM agents ingest EDGAR/Finnhub/RSS = real prompt-injection surface, currently undefended. hood now has a handoff for this.
**Adopt:** write a `GROK_HANDOFF_SECURITY_INJECTION.md` modeled on `../hood_agent_1/GROK_HANDOFF_SECURITY_INJECTION.md` — delimit + label untrusted source text into every LLM prompt, fail-safe schema validation, bound/sanitize fetched text. Required before live (tracked in DEFINITION_OF_DONE.md).

## CL-6 — Runner discipline reference
**Why:** your runner is more mature than hood's, but pure_arb's observer/runner separation + two-phase executor are still the cleanest reference if you extend the live loop.
**Reference:** `../pure_arb_bot/` `execution/`, `scripts/`.

## Sequencing
CL-1 + CL-4 first (calibration + no-LLM-sizing are graduation-blocking and your own assessment already commits to them). CL-2 locks them. CL-5 before any real capital. CL-3 ongoing.
