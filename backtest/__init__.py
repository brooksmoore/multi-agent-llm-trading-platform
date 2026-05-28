"""Backtesting + rules-only baseline.

This package answers the only question that matters for the project's stated
goal — "is the LLM earning its keep net of costs?" — by running a deterministic,
LLM-free baseline through the same signal math the live Haiku sleeve uses, and
comparing it to SPY buy-and-hold over historical data.

It deliberately does NOT replay the full live OMS/RiskGate/kill-switch stack:
that is a much larger effort. The boundary is explicit — see engine.py — so the
numbers here are a research control, not a claim that live execution matches.
"""
