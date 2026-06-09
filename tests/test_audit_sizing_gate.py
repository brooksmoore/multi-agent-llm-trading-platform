"""AUDITOR-OWNED — Grok (builder) must NOT edit this file.

Invariant gates for the sizing module. Each test here defends a non-negotiable
architectural rule from WORKFLOW.md §6, GROK_HANDOFF_CROSS_LEARNING.md CL-1, and
DEFINITION_OF_DONE.md § Least agency. If any test here fails, it is a regression
against a first-principle — fix the code, not the test.

See LEDGER.md Audit 001 OPEN-4 for the rationale for this file.
"""

from __future__ import annotations

from decimal import Decimal


# ── CL-1: LLM is weight-only, never sizes on its own confidence ───────────────
# Source: GROK_HANDOFF_CROSS_LEARNING.md CL-1 + WORKFLOW.md §6 rule 2.
# "LLM-stated confidence is not a calibrated probability (proven by kalshi_1.0's
# death). Python sizes; the model filters."


def test_sizing_functions_never_accept_conviction_parameter() -> None:
    """Conviction (0-10 LLM scalar) must not appear in any sizing function signature.

    The full call chain is: Intent.conviction → memory.record_intent() (logging only).
    It must not reach vol_targeted_position_value or effective_max_gross as a
    parameter or as an implicit multiplier hidden in *args/**kwargs.
    """
    import inspect

    from execution.sizing import effective_max_gross, vol_targeted_position_value

    for fn in (vol_targeted_position_value, effective_max_gross):
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        assert "conviction" not in params, (
            f"{fn.__name__} must not accept 'conviction' — "
            "LLM confidence must never scale a position size (CL-1)"
        )
        assert "conviction" not in str(sig), (
            f"{fn.__name__} signature string contains 'conviction' — "
            "check for *args/**kwargs tricks"
        )


def test_vol_targeted_position_value_is_conviction_free_in_practice() -> None:
    """A concrete call with only weight/equity/vol params must produce a valid result.

    If conviction were required, this call would fail. The value of the test is
    proving the function is callable without ANY conviction argument.
    """
    from execution.sizing import vol_targeted_position_value

    val = vol_targeted_position_value(
        target_weight=Decimal("0.10"),
        agent_equity=Decimal("30000"),
        realized_vol_annual=Decimal("0.18"),
        effective_vol_target=Decimal("0.12"),
    )
    assert val > Decimal("0"), "sizing must return positive notional for valid inputs"
