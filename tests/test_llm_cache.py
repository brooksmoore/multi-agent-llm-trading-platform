"""Live cache-hit regression test for every rendered system prompt.

`test_llm_cache_ttl.py` covers the *mechanism* offline (asserting the SDK
call carries `cache_control: {ttl: "1h"}`). This file covers the
*prefix-length sufficiency* live: it makes two real calls per prompt
(30s apart) and asserts both `cache_creation_input_tokens > 0` on the
prime call and `cache_read_input_tokens > 0` on the second call.

Why both: the SDK will happily accept any-length system block with
`cache_control` set, but Anthropic only writes a cache entry when the
block exceeds the model's minimum prefix length. Verified empirically
2026-05-11 against Anthropic's published thresholds:

    claude-haiku-4-5      4,096 tokens
    claude-sonnet-4-6     2,048 tokens
    claude-opus-4-7       4,096 tokens

Without this test, a future prompt edit can silently drop a prefix
back below threshold and we'd notice only when daily spend doubles.

Skipped automatically when ANTHROPIC_API_KEY is unset, so CI runs
that don't have a key (or that don't want to pay) skip cleanly.
A single local run after any `agents/prompts/*.md` edit is enough.
"""

from __future__ import annotations

import os
import pathlib
import time
from datetime import UTC, datetime
from decimal import Decimal

import anthropic
import pytest

from agents.base import AgentState, render_system_prompt
from agents.llm import HAIKU_MODEL, OPUS_MODEL, SONNET_MODEL
from core.types import KillSwitchState
from execution.broker import BrokerAccount

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set; live cache test skipped",
    ),
]

# (prompt_path, model_id_for_cache_validation)
# The Manager prompt is validated against Opus (its default model). The Sonnet
# downgrade path (T2.1) trivially passes since the prompt also clears the
# Sonnet 4.6 threshold (2,048).
_PROMPTS: list[tuple[str, str]] = [
    ("agents/prompts/haiku_agent.md", HAIKU_MODEL),
    ("agents/prompts/sonnet_agent.md", SONNET_MODEL),
    ("agents/prompts/opus_agent.md", OPUS_MODEL),
    ("agents/prompts/manager_agent.md", OPUS_MODEL),
    # T2.2: NewsScorer prompt; uses Haiku, threshold 4,096.
    ("agents/prompts/news_scorer.md", HAIKU_MODEL),
]


def _empty_state() -> AgentState:
    return AgentState(
        timestamp=datetime.now(UTC),
        bars_by_symbol={},
        news=[],
        positions=[],
        account=BrokerAccount(
            cash=Decimal("1000"),
            equity=Decimal("1000"),
            buying_power=Decimal("1000"),
            pattern_day_trader=False,
            daytrade_count=0,
        ),
        kill_switch_state=KillSwitchState.OK,
        master_capability=Decimal("1.0"),
        effective_max_gross=Decimal("1.0"),
    )


def test_all_rendered_prompts_cache_with_their_target_models() -> None:
    """Every rendered system prompt must write+read its cache on its target model.

    Two phases share one 30s sleep so the whole test runs in ~60s total
    rather than 30s × N prompts.
    """
    state = _empty_state()
    rendered = {
        path: render_system_prompt(pathlib.Path(path).read_text(), state)
        for path, _ in _PROMPTS
    }
    client = anthropic.Anthropic()

    # Phase 1 — touch each cache. Either cache_creation > 0 (cache was empty
    # and just got written) or cache_read > 0 (a prior call within the 1h TTL
    # already populated it). Both prove the prompt cleared the model's
    # minimum cacheable prefix length. Only sum=0 fails.
    prime_failures: list[str] = []
    for path, model in _PROMPTS:
        r = client.messages.create(
            model=model,
            max_tokens=32,
            system=[{
                "type": "text",
                "text": rendered[path],
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=[{"role": "user", "content": "Reply with the digit 1 only."}],
        )
        cc = r.usage.cache_creation_input_tokens or 0
        cr = r.usage.cache_read_input_tokens or 0
        if cc + cr <= 0:
            prime_failures.append(
                f"{path} ({model}): both cache_creation_input_tokens and "
                f"cache_read_input_tokens are 0 on prime call. Prompt is "
                f"below the model's minimum cacheable prefix length. Pad "
                f"the prompt with additional static content."
            )

    assert not prime_failures, "Cache PRIME failures:\n  " + "\n  ".join(prime_failures)

    # Wait once for all four. Within 1h TTL window with margin.
    time.sleep(30)

    # Phase 2 — second call per prompt. Assert cache_read > 0 (proof the
    # write actually persisted and is being read back).
    read_failures: list[str] = []
    for path, model in _PROMPTS:
        r = client.messages.create(
            model=model,
            max_tokens=32,
            system=[{
                "type": "text",
                "text": rendered[path],
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=[{"role": "user", "content": "Reply with the digit 2 only."}],
        )
        cr = r.usage.cache_read_input_tokens or 0
        if cr <= 0:
            read_failures.append(
                f"{path} ({model}): cache_read_input_tokens={cr} on second call. "
                f"Cache was primed but not read back — investigate cache_control "
                f"wiring or ttl configuration."
            )

    assert not read_failures, "Cache READ failures:\n  " + "\n  ".join(read_failures)
