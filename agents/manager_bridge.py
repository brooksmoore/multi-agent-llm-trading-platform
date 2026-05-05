"""Bridge: Manager outputs → sleeve agent context + sizing layer.

Without this module, the Manager talks to itself. The sleeve agents have
fields on `AgentState` (`manager_regime_text`, `manager_critique`,
`manager_directive`, `manager_morning_brief`) that they read in their
prompts, but nothing populates them. The capital_reallocation output is
similarly persisted but never consumed by the planner.

This module is the single read/write surface for all Manager → system
plumbing:

  * read/write per-agent Manager directives (drawdown response, etc.)
  * read/write the daily morning brief
  * read/write the weekly regime read
  * read/write sleeve-weight reallocation (consumed by sizing.py)

All values are persisted: manager.db memory keys for prose, a JSON file
for sleeve weights (so sizing can read it without a DB connection).
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

from core.types import AgentId

log = logging.getLogger(__name__)

# Memory keys used in manager.db ----------------------------------------------
KEY_LAST_REGIME = "last_regime_read"
KEY_LAST_BRIEF = "last_morning_brief"
KEY_DRAWDOWN_DIRECTIVE = "drawdown_directive:{agent}"   # per-sleeve
KEY_LAST_CRITIQUE = "last_adversarial_critique:{agent}"  # per-sleeve

# Sleeve-weight reallocation file (read by sizing.py, written by Friday job).
SLEEVE_WEIGHTS_FILE = Path("data/manager_sleeve_weights.json")


# ── Read/write: prose directives ─────────────────────────────────────────────


def read_manager_context(manager_mem: object, agent_id: AgentId) -> dict[str, str]:
    """Return all Manager outputs relevant to one sleeve agent.

    Returns a dict with four string keys (always present, possibly empty):
        regime_text       — last weekly regime read summary
        morning_brief     — today's premarket brief
        drawdown_directive — Manager's directive after the agent's most recent
                              drawdown bucket transition (empty if no drawdown)
        critique          — most recent adversarial critique on this agent
    """
    out = {
        "regime_text": "",
        "morning_brief": "",
        "drawdown_directive": "",
        "critique": "",
    }
    try:
        recall = getattr(manager_mem, "recall", None)
        if recall is None:
            return out
        agent_short = str(agent_id).split(".")[-1].lower()
        regime_raw = recall(KEY_LAST_REGIME) or ""
        out["regime_text"] = _extract_regime_summary(regime_raw)
        out["morning_brief"] = (recall(KEY_LAST_BRIEF) or "")[:1500]
        out["drawdown_directive"] = (
            recall(KEY_DRAWDOWN_DIRECTIVE.format(agent=agent_short)) or ""
        )[:600]
        out["critique"] = (
            recall(KEY_LAST_CRITIQUE.format(agent=agent_short)) or ""
        )[:600]
    except Exception:
        log.warning("manager_bridge.read_manager_context failed", exc_info=True)
    return out


def write_morning_brief(manager_mem: object, brief: str) -> None:
    """Persist today's morning brief. Truncated at 1.5kB to stay cache-friendly."""
    try:
        getattr(manager_mem, "remember")(KEY_LAST_BRIEF, brief[:1500])
    except Exception:
        log.warning("manager_bridge.write_morning_brief failed", exc_info=True)


def write_drawdown_directive(
    manager_mem: object, agent_id: AgentId, directive: str
) -> None:
    """Persist a per-sleeve drawdown directive issued by the Manager."""
    try:
        agent_short = str(agent_id).split(".")[-1].lower()
        getattr(manager_mem, "remember")(
            KEY_DRAWDOWN_DIRECTIVE.format(agent=agent_short), directive[:600]
        )
    except Exception:
        log.warning("manager_bridge.write_drawdown_directive failed", exc_info=True)


def write_adversarial_critique(
    manager_mem: object, agent_id: AgentId, critique: str
) -> None:
    """Persist a per-sleeve adversarial critique."""
    try:
        agent_short = str(agent_id).split(".")[-1].lower()
        getattr(manager_mem, "remember")(
            KEY_LAST_CRITIQUE.format(agent=agent_short), critique[:600]
        )
    except Exception:
        log.warning("manager_bridge.write_adversarial_critique failed", exc_info=True)


# ── Read/write: sleeve weights (consumed by sizing.effective_max_gross) ──────


def read_sleeve_weights() -> dict[AgentId, Decimal]:
    """Return the latest Manager-set sleeve-weight multipliers, or {} if none.

    Multipliers stack with the existing base × MC × VIX × DD ladder in the
    sizing formula: an agent at multiplier 1.2 gets +20% gross-cap headroom,
    0.8 gets -20%. Default 1.0 (no change) for any agent not in the file.
    """
    if not SLEEVE_WEIGHTS_FILE.exists():
        return {}
    try:
        raw = json.loads(SLEEVE_WEIGHTS_FILE.read_text())
        out: dict[AgentId, Decimal] = {}
        for k, v in raw.items():
            try:
                aid = AgentId(k.lower())
            except ValueError:
                continue
            try:
                out[aid] = Decimal(str(v))
            except Exception:
                continue
        return out
    except Exception:
        log.warning("manager_bridge.read_sleeve_weights failed", exc_info=True)
        return {}


def write_sleeve_weights(weights: dict[AgentId, Decimal]) -> None:
    """Atomically replace the sleeve-weights file from a Manager reallocation."""
    try:
        SLEEVE_WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(aid).split(".")[-1].lower(): str(w) for aid, w in weights.items()}
        tmp = SLEEVE_WEIGHTS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(SLEEVE_WEIGHTS_FILE)
    except Exception:
        log.warning("manager_bridge.write_sleeve_weights failed", exc_info=True)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _extract_regime_summary(regime_raw: str) -> str:
    """Pull a concise summary out of the persisted regime_read JSON.

    The Manager persists `regime_read.json` payload as a stringified dict.
    Sleeve agents only need the human-readable lines, not the entire schema.
    """
    if not regime_raw:
        return ""
    try:
        parsed = json.loads(regime_raw)
    except Exception:
        return regime_raw[:600]
    if not isinstance(parsed, dict):
        return regime_raw[:600]
    # Best-effort: prefer summary-style keys, fall back to a compact rendering.
    for key in ("summary", "regime_text", "headline", "narrative"):
        v = parsed.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:600]
    # Fall back: render top-level keys as label: value lines.
    lines: list[str] = []
    for k, v in parsed.items():
        if isinstance(v, (str, int, float)) and len(str(v)) < 200:
            lines.append(f"{k}: {v}")
        if len(lines) >= 6:
            break
    return "\n".join(lines)[:600]
