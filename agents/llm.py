"""Anthropic LLM wrapper: budget gating, retry, prompt caching, AgentMemo logging."""

from __future__ import annotations

import hashlib
import logging
import random
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anthropic

from core.types import AgentId, AgentMemo, IntentId  # noqa: F401  (IntentId re-exported)
from execution.budget import BudgetLedger

log = logging.getLogger(__name__)

# Pricing per 1K tokens: (input, cache_write, cache_hit, output)
# Cache write = 125% of input; cache hit = 10% of input (ephemeral cache)
_PRICING: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {
    "claude-haiku-4-5-20251001": (
        Decimal("0.00080"),
        Decimal("0.00100"),
        Decimal("0.00008"),
        Decimal("0.00400"),
    ),
    "claude-sonnet-4-6": (
        Decimal("0.00300"),
        Decimal("0.00375"),
        Decimal("0.00030"),
        Decimal("0.01500"),
    ),
    "claude-opus-4-7": (
        Decimal("0.01500"),
        Decimal("0.01875"),
        Decimal("0.00150"),
        Decimal("0.07500"),
    ),
}

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"
OPUS_MODEL = "claude-opus-4-7"

# Backoff schedule for Anthropic 529 (overloaded): [1s, 4s, 16s] + 0–1s jitter.
# Applied per-attempt independently of the 429 retry counter.
_BACKOFF_529_SECS: list[float] = [1.0, 4.0, 16.0]


class BudgetExhausted(Exception):
    """Raised before any API call when the estimated cost exceeds the remaining budget."""


def _cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_hit_tokens: int = 0,
) -> Decimal:
    p_in, p_cw, p_ch, p_out = _PRICING.get(model, _PRICING[HAIKU_MODEL])
    return (
        Decimal(input_tokens) * p_in
        + Decimal(cache_write_tokens) * p_cw
        + Decimal(cache_hit_tokens) * p_ch
        + Decimal(output_tokens) * p_out
    ) / Decimal("1000")


class LLMClient:
    """Wraps the Anthropic SDK with budget gating, retries, and prompt caching."""

    def __init__(
        self,
        budget: BudgetLedger,
        model: str = HAIKU_MODEL,
        api_key: str | None = None,
        max_retries: int = 3,
    ) -> None:
        self._budget = budget
        self._model = model
        self._max_retries = max_retries
        self._client = (
            anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        )

    def call(
        self,
        system: str,
        user: str,
        agent_id: AgentId,
        call_type: str,
        max_tokens: int = 1024,
        use_caching: bool = True,
    ) -> tuple[str, AgentMemo]:
        """Call the model and return (response_text, AgentMemo).

        Raises BudgetExhausted if the estimated cost exceeds remaining budget.
        """
        est_input = (len(system) + len(user)) // 4
        est_cost = _cost(self._model, est_input, max_tokens)
        if est_cost > self._budget.remaining():
            raise BudgetExhausted(
                f"Estimated cost {est_cost:.6f} USD exceeds remaining budget "
                f"{self._budget.remaining():.6f} USD"
            )

        prompt_hash = hashlib.sha256(f"{system}\n{user}".encode()).hexdigest()
        ts = datetime.now(UTC)

        if use_caching:
            system_param: str | list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ]
        else:
            system_param = system

        response = self._call_with_retry(system_param, user, max_tokens)

        text: str = response.content[0].text  # type: ignore[union-attr]

        usage = response.usage
        in_tok: int = usage.input_tokens
        out_tok: int = usage.output_tokens
        cache_write: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_hit: int = getattr(usage, "cache_read_input_tokens", 0) or 0

        actual_cost = _cost(self._model, in_tok, out_tok, cache_write, cache_hit)
        self._budget.record_spend(str(agent_id), actual_cost, call_type, ts)

        memo = AgentMemo(
            id=uuid.uuid4(),
            agent_id=agent_id,
            call_type=call_type,
            model=self._model,
            timestamp=ts,
            cached_tokens=cache_hit,
            new_input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=actual_cost,
            prompt_hash=prompt_hash,
            response_json=text,
            intents_emitted=0,
        )

        log.info(
            "LLM call agent=%s type=%s cost=$%.6f tokens(in=%d cached=%d out=%d)",
            agent_id,
            call_type,
            actual_cost,
            in_tok,
            cache_hit,
            out_tok,
        )

        return text, memo

    def _call_with_retry(
        self,
        system: str | list[dict[str, Any]],
        user: str,
        max_tokens: int,
    ) -> anthropic.types.Message:
        """Attempt the API call up to max_retries times with backoff.

        - 429 RateLimitError: exponential backoff 2^attempt + 0–1s jitter.
        - 529 APIStatusError (Anthropic overloaded): [1, 4, 16]s + 0–1s jitter.
        - Other 5xx APIStatusError: flat 1s sleep per attempt.
        """
        last_exc: BaseException = RuntimeError("no attempts made")
        for attempt in range(self._max_retries):
            try:
                return self._client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    system=system,  # type: ignore[arg-type]
                    messages=[{"role": "user", "content": user}],
                )
            except anthropic.RateLimitError as exc:
                last_exc = exc
                if attempt >= self._max_retries - 1:
                    raise
                # Exponential backoff with jitter to avoid thundering herd
                # when multiple agents hit a 429 simultaneously.
                time.sleep(2**attempt + random.uniform(0, 1))
            except anthropic.APIStatusError as exc:
                last_exc = exc
                if attempt >= self._max_retries - 1:
                    raise
                if getattr(exc, "status_code", None) == 529 and attempt < len(_BACKOFF_529_SECS):
                    time.sleep(_BACKOFF_529_SECS[attempt] + random.uniform(0, 1))
                else:
                    time.sleep(1.0)
        raise last_exc
