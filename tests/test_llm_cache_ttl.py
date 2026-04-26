"""Assert that every cached LLM call carries cache_control: {type: ephemeral, ttl: 1h}.

Without the explicit ttl Anthropic silently defaults to 5 minutes, which is ~12x
more expensive than budgeted across hour boundaries (non-negotiable rule #2).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic

from agents.llm import HAIKU_MODEL, LLMClient
from core.types import AgentId
from execution.budget import BudgetLedger


def _make_client(tmp_path: Path) -> LLMClient:
    budget = BudgetLedger(tmp_path / "spend.json")
    return LLMClient(budget=budget, model=HAIKU_MODEL, api_key="test-key")


def _fake_response(input_tokens: int = 10, output_tokens: int = 5) -> MagicMock:
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    content = MagicMock()
    content.text = '{"ok": true}'
    resp = MagicMock(spec=anthropic.types.Message)
    resp.content = [content]
    resp.usage = usage
    return resp


def test_cache_control_has_ttl_1h() -> None:
    """The SDK call must include cache_control with ttl='1h' when use_caching=True."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _make_client(Path(tmp))
        with patch.object(
            client._client.messages, "create", return_value=_fake_response()
        ) as mock_create:
            client.call(
                system="System prompt",
                user="User message",
                agent_id=AgentId.HAIKU,
                call_type="test",
                max_tokens=64,
                use_caching=True,
            )

        system_param = mock_create.call_args.kwargs.get("system")

        assert isinstance(system_param, list), "system should be a list when use_caching=True"
        assert len(system_param) == 1
        block = system_param[0]
        assert "cache_control" in block, "cache_control block missing from system message"
        cc = block["cache_control"]
        assert cc.get("type") == "ephemeral", f"Expected type=ephemeral, got {cc}"
        assert cc.get("ttl") == "1h", (
            f"cache_control must have ttl='1h' (Anthropic default is 5m, which is 12x more "
            f"expensive across hour boundaries). Got: {cc}"
        )


def test_cache_control_ttl_present_on_every_cached_call() -> None:
    """Multiple calls should ALL carry the ttl field — regression guard."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _make_client(Path(tmp))
        with patch.object(
            client._client.messages, "create", return_value=_fake_response()
        ) as mock_create:
            for call_type in ("observe", "deep_dive", "news_scan"):
                client.call(
                    system=f"System for {call_type}",
                    user=f"User for {call_type}",
                    agent_id=AgentId.HAIKU,
                    call_type=call_type,
                    max_tokens=64,
                    use_caching=True,
                )

        assert mock_create.call_count == 3
        for i, call in enumerate(mock_create.call_args_list):
            system_param = call.kwargs.get("system")
            assert isinstance(system_param, list), f"call {i}: system not a list"
            cc = system_param[0]["cache_control"]
            assert cc.get("ttl") == "1h", (
                f"call {i}: missing ttl='1h' in cache_control"
            )


def test_no_cache_control_when_caching_disabled() -> None:
    """When use_caching=False the system param is a plain string (no cache block)."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _make_client(Path(tmp))
        with patch.object(
            client._client.messages, "create", return_value=_fake_response()
        ) as mock_create:
            client.call(
                system="System prompt",
                user="User message",
                agent_id=AgentId.HAIKU,
                call_type="test",
                max_tokens=64,
                use_caching=False,
            )

        system_param = mock_create.call_args.kwargs.get("system")
        assert isinstance(system_param, str), (
            "When use_caching=False, system should be a plain string"
        )
