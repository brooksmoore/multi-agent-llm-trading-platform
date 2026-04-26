"""Shared helpers for parsing JSON returned by LLMs."""

from __future__ import annotations

import json
from typing import Any


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort parse of a JSON object from an LLM response.

    Tries a strict parse first, then falls back to extracting the first
    {...} substring. Returns None if neither succeeds.
    """
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            result = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return result if isinstance(result, dict) else None
