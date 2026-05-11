"""Signal-fingerprint tests: Opus.signal_fingerprint and the Manager helper.

T1.4 / Plan 2c. Skipping a no-op LLM cycle saves $0.005-0.015 per skipped
call; these tests guard against regressions where the fingerprint either
becomes too sticky (skips real signal changes) or too volatile (never
skips).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from agents.base import AgentState
from agents.llm import LLMClient
from agents.manager_agent import compute_manager_fingerprint
from agents.memory import AgentMemory
from agents.opus_agent import TARGET_HOLDINGS, OpusAgent
from core.types import AgentId, AssetClass, DrawdownBucket, KillSwitchState, VixBucket
from execution.broker import BrokerAccount, BrokerPosition

_TS = datetime(2026, 5, 11, 16, 30, tzinfo=UTC)


def _account() -> BrokerAccount:
    return BrokerAccount(
        cash=Decimal("1000"), equity=Decimal("1000"),
        buying_power=Decimal("1000"),
        pattern_day_trader=False, daytrade_count=0,
    )


def _position(symbol: str, qty: str) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol, qty=Decimal(qty),
        avg_entry_price=Decimal("100"), current_price=Decimal("100"),
        asset_class=AssetClass.EQUITY,
    )


def _state(
    positions: list[BrokerPosition] | None = None,
    emg: str = "1.0",
    directive: str = "",
) -> AgentState:
    return AgentState(
        timestamp=_TS, bars_by_symbol={}, news=[],
        positions=positions or [],
        account=_account(),
        kill_switch_state=KillSwitchState.OK,
        master_capability=Decimal("1.0"),
        effective_max_gross=Decimal(emg),
        manager_directive=directive,
    )


def _opus() -> OpusAgent:
    mock_llm = MagicMock(spec=LLMClient)
    mem = AgentMemory(":memory:", AgentId.OPUS)
    return OpusAgent(llm=mock_llm, memory=mem)


# ── Opus.signal_fingerprint ───────────────────────────────────────────────────


def test_opus_fingerprint_returns_none_in_initiation_mode() -> None:
    """Initiation mode (holdings under target) must always run; no skipping."""
    agent = _opus()
    # Empty book: clearly < TARGET_HOLDINGS
    assert agent.signal_fingerprint(_state(positions=[])) is None

    # One holding short of target: still initiation
    positions = [_position(f"SYM{i}", "10") for i in range(TARGET_HOLDINGS - 1)]
    assert agent.signal_fingerprint(_state(positions=positions)) is None


def test_opus_fingerprint_stable_across_identical_state() -> None:
    """Two calls with identical state produce identical fingerprints."""
    agent = _opus()
    positions = [_position(f"SYM{i}", "10") for i in range(TARGET_HOLDINGS)]
    s = _state(positions=positions, emg="1.0", directive="hold tight")

    fp1 = agent.signal_fingerprint(s)
    fp2 = agent.signal_fingerprint(s)
    assert fp1 is not None
    assert fp1 == fp2


def test_opus_fingerprint_changes_on_holdings_change() -> None:
    """Adding a position invalidates the fingerprint."""
    agent = _opus()
    base = [_position(f"SYM{i}", "10") for i in range(TARGET_HOLDINGS)]
    fp_before = agent.signal_fingerprint(_state(positions=base))

    after = [*base, _position("EXTRA", "5")]
    fp_after = agent.signal_fingerprint(_state(positions=after))

    assert fp_before != fp_after


def test_opus_fingerprint_changes_on_emg_change() -> None:
    """A new effective_max_gross (e.g. VIX cap cut) invalidates the fingerprint."""
    agent = _opus()
    positions = [_position(f"SYM{i}", "10") for i in range(TARGET_HOLDINGS)]

    fp_before = agent.signal_fingerprint(_state(positions=positions, emg="1.0"))
    fp_after = agent.signal_fingerprint(_state(positions=positions, emg="0.75"))

    assert fp_before != fp_after


def test_opus_fingerprint_changes_on_manager_directive_change() -> None:
    """A new Manager directive invalidates the fingerprint."""
    agent = _opus()
    positions = [_position(f"SYM{i}", "10") for i in range(TARGET_HOLDINGS)]

    fp_before = agent.signal_fingerprint(_state(positions=positions, directive=""))
    fp_after = agent.signal_fingerprint(_state(positions=positions, directive="trim TSM"))

    assert fp_before != fp_after


def test_opus_fingerprint_stable_across_micro_qty_drift() -> None:
    """Sub-cent qty drift (Decimal noise) must not invalidate the fingerprint."""
    agent = _opus()
    base = [_position(f"SYM{i}", "10") for i in range(TARGET_HOLDINGS)]
    fp_before = agent.signal_fingerprint(_state(positions=base))

    # Replace one position with qty 10.001 — should round to 10.00 in fingerprint
    drifted = [_position(f"SYM{i}", "10.001" if i == 0 else "10") for i in range(TARGET_HOLDINGS)]
    fp_after = agent.signal_fingerprint(_state(positions=drifted))

    assert fp_before == fp_after


def test_opus_fingerprint_changes_on_watchlist_change() -> None:
    """Watchlist additions invalidate the fingerprint."""
    agent = _opus()
    positions = [_position(f"SYM{i}", "10") for i in range(TARGET_HOLDINGS)]
    s = _state(positions=positions)

    fp_before = agent.signal_fingerprint(s)
    agent._memory.remember("opus:watchlist", "ANET,PANW")  # noqa: SLF001
    fp_after = agent.signal_fingerprint(s)

    assert fp_before != fp_after


# ── Manager fingerprint helper ────────────────────────────────────────────────


def test_manager_fingerprint_stable_across_identical_inputs() -> None:
    """Two calls with the same macro inputs produce identical fingerprints."""
    dd = {
        AgentId.HAIKU: DrawdownBucket.NORMAL,
        AgentId.SONNET: DrawdownBucket.NORMAL,
        AgentId.OPUS: DrawdownBucket.NORMAL,
    }
    fp1 = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000"), dd)
    fp2 = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000"), dd)
    assert fp1 == fp2


def test_manager_fingerprint_changes_on_vix_bucket_change() -> None:
    """A VIX bucket transition invalidates the strategic-call cache."""
    dd = {
        AgentId.HAIKU: DrawdownBucket.NORMAL,
        AgentId.SONNET: DrawdownBucket.NORMAL,
        AgentId.OPUS: DrawdownBucket.NORMAL,
    }
    fp_very_low = compute_manager_fingerprint(VixBucket.VERY_LOW, Decimal("3000"), dd)
    fp_sweet = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000"), dd)
    fp_elevated = compute_manager_fingerprint(VixBucket.ELEVATED, Decimal("3000"), dd)

    assert fp_very_low != fp_sweet
    assert fp_sweet != fp_elevated


def test_manager_fingerprint_changes_on_drawdown_bucket_change() -> None:
    """Any sleeve entering a worse drawdown bucket invalidates the fingerprint."""
    base_dd = {
        AgentId.HAIKU: DrawdownBucket.NORMAL,
        AgentId.SONNET: DrawdownBucket.NORMAL,
        AgentId.OPUS: DrawdownBucket.NORMAL,
    }
    fp_before = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000"), base_dd)

    yellow_dd = {**base_dd, AgentId.OPUS: DrawdownBucket.YELLOW}
    fp_after = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000"), yellow_dd)

    assert fp_before != fp_after


def test_manager_fingerprint_stable_across_subdollar_equity_drift() -> None:
    """Sub-dollar equity drift (mark-to-market noise) must not invalidate."""
    dd = {
        AgentId.HAIKU: DrawdownBucket.NORMAL,
        AgentId.SONNET: DrawdownBucket.NORMAL,
        AgentId.OPUS: DrawdownBucket.NORMAL,
    }
    fp1 = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000.42"), dd)
    fp2 = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000.49"), dd)
    assert fp1 == fp2


def test_manager_fingerprint_changes_on_dollar_level_equity_change() -> None:
    """Equity changes at the whole-dollar level DO invalidate."""
    dd = {
        AgentId.HAIKU: DrawdownBucket.NORMAL,
        AgentId.SONNET: DrawdownBucket.NORMAL,
        AgentId.OPUS: DrawdownBucket.NORMAL,
    }
    fp1 = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000"), dd)
    fp2 = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3010"), dd)
    assert fp1 != fp2


def test_manager_fingerprint_handles_unknown_vix() -> None:
    """No VIX read available: helper still produces a stable fingerprint."""
    dd = {
        AgentId.HAIKU: DrawdownBucket.NORMAL,
        AgentId.SONNET: DrawdownBucket.NORMAL,
        AgentId.OPUS: DrawdownBucket.NORMAL,
    }
    fp1 = compute_manager_fingerprint(None, Decimal("3000"), dd)
    fp2 = compute_manager_fingerprint(None, Decimal("3000"), dd)
    assert fp1 == fp2
    # And distinct from the with-VIX case:
    fp_with_vix = compute_manager_fingerprint(VixBucket.SWEET_SPOT, Decimal("3000"), dd)
    assert fp1 != fp_with_vix
