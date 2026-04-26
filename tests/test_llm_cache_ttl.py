"""Assert that every cached LLM call carries cache_control: {type: ephemeral, ttl: 1h}.

Without the explicit ttl Anthropic silently defaults to 5 minutes, which is ~12x
more expensive than budgeted across hour boundaries (non-negotiable rule #2).

Also tests that 529 (Anthropic overloaded) retries use [1, 4, 16]s backoff.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import httpx

from agents.llm import _BACKOFF_529_SECS, HAIKU_MODEL, LLMClient
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


# ── 529 overloaded retry tests ────────────────────────────────────────────────


def _make_529_error() -> anthropic.APIStatusError:
    """Build an APIStatusError with status_code=529 (Anthropic overloaded)."""
    response = httpx.Response(529, request=httpx.Request("POST", "https://api.anthropic.com"))
    return anthropic.APIStatusError("overloaded", response=response, body=None)


def test_529_retries_and_succeeds() -> None:
    """After two 529 errors the client succeeds on the third attempt."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _make_client(Path(tmp))
        client._max_retries = 4  # allow 4 attempts so [1, 4, 16] backoff can apply

        side_effects = [_make_529_error(), _make_529_error(), _fake_response()]
        with (
            patch.object(
                client._client.messages, "create", side_effect=side_effects
            ) as mock_create,
            patch("agents.llm.time.sleep") as mock_sleep,
        ):
            text, memo = client.call(
                system="System",
                user="User",
                agent_id=AgentId.HAIKU,
                call_type="test",
                max_tokens=64,
            )

        assert text == '{"ok": true}'
        assert mock_create.call_count == 3
        # First sleep: _BACKOFF_529_SECS[0] + jitter; second: _BACKOFF_529_SECS[1] + jitter
        assert mock_sleep.call_count == 2
        first_sleep_duration = mock_sleep.call_args_list[0][0][0]
        second_sleep_duration = mock_sleep.call_args_list[1][0][0]
        assert _BACKOFF_529_SECS[0] <= first_sleep_duration < _BACKOFF_529_SECS[0] + 1.0
        assert _BACKOFF_529_SECS[1] <= second_sleep_duration < _BACKOFF_529_SECS[1] + 1.0


def test_529_raises_after_max_retries() -> None:
    """Exhausting all retries on 529 propagates the final APIStatusError."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _make_client(Path(tmp))
        client._max_retries = 2  # only 2 attempts

        side_effects = [_make_529_error(), _make_529_error()]
        with (
            patch.object(client._client.messages, "create", side_effect=side_effects),
            patch("agents.llm.time.sleep"),
        ):
            import pytest  # noqa: PLC0415
            with pytest.raises(anthropic.APIStatusError):
                client.call(
                    system="System",
                    user="User",
                    agent_id=AgentId.HAIKU,
                    call_type="test",
                    max_tokens=64,
                )
