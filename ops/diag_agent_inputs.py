"""One-shot: print exactly what haiku/sonnet/opus see right now.

Run: uv run python ops/diag_agent_inputs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import App
from config.settings import Settings
from core.types import AgentId


def main() -> int:
    settings = Settings()
    # Use a temp DB path so we don't collide with the running bot's locks.
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="diag_"))
    app = App(
        settings,
        oms_db_path=tmp / "oms.db",
        budget_path=tmp / "budget.json",
        memory_dir=tmp / "memory",
        heartbeat_path=tmp / "heartbeat.json",
        logs_dir=tmp,
        run_recover_on_start=False,
    )

    haiku_state = app.build_agent_state(agent_id=AgentId.HAIKU)
    sonnet_state = app.build_agent_state(agent_id=AgentId.SONNET)
    opus_state = app.build_agent_state(agent_id=AgentId.OPUS)

    print("=" * 78)
    print("UNIVERSE FETCHED:", list(haiku_state.bars_by_symbol.keys()))
    print("BAR COUNTS PER SYMBOL:")
    for sym, bars in haiku_state.bars_by_symbol.items():
        print(f"  {sym:8} : {len(bars):4} bars")
    vix_str = f"{float(haiku_state.vix_value):.2f}" if haiku_state.vix_value is not None else "n/a"
    print(f"VIX value: {vix_str}")
    print()

    print("=" * 78)
    print("HAIKU SEES:")
    print("=" * 78)
    print(app.haiku._format_context(  # type: ignore[attr-defined]
        haiku_state,
        app.haiku._compute_equity_trend(haiku_state.bars_by_symbol),  # type: ignore[attr-defined]
        app.haiku._compute_crypto_trend(haiku_state.bars_by_symbol),  # type: ignore[attr-defined]
    ))
    print()

    print("=" * 78)
    print("SONNET SEES:")
    print("=" * 78)
    print(app.sonnet._format_context(  # type: ignore[attr-defined]
        sonnet_state,
        app.sonnet._compute_factor_signals(sonnet_state.bars_by_symbol),  # type: ignore[attr-defined]
    ))
    print()

    print("=" * 78)
    print("OPUS SEES (daily check):")
    print("=" * 78)
    print(app.opus._format_daily_context(opus_state))  # type: ignore[attr-defined]

    app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
