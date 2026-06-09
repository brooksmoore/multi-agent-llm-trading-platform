# CLAUDE.md — context for Multi_Agent_Asset_Competitive_Bot

Claude acts as auditor/overseer for this agent (Grok Build implements). Read this folder's spec + ledger files before any audit or change.
---

## STATUS.md upkeep (standing rule — every session)

This folder has a `STATUS.md` with a standardized header (one-liner, stage, live gate, tests, intelligence type, single most important next thing, honest odds, last-updated date). It is the at-a-glance map for this agent and feeds the portfolio dashboard.

**At the END of every working session, before you finish, update `STATUS.md`:**
1. Bump **Last updated** to today's date.
2. Update any header field that changed this session — especially **Stage**, **Tests** (current passing count), and **Single most important next thing**.
3. If the live gate flipped, update **Live gate** (this should never happen without an explicit human decision).
4. Add a one-line entry under **Recent movement** describing what changed this session (what / why).
5. Do NOT rewrite the detailed ledger here — keep deep history in the existing ledger/changelog/handoff files. STATUS.md stays short.

Stage vocabulary: `idea → skeleton → core-done → runner-wiring → paper-validating → live-gated → live`.

Keep the header honest. If something regressed, say so. The dashboard is only useful if this file is true.
