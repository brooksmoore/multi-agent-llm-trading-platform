#!/usr/bin/env python
"""Robinhood agentic MCP probe — STEP 2 of the live-broker bring-up.

Read-only. Connects to Robinhood's MCP endpoint, runs the `initialize`
handshake, lists the advertised tools with their input schemas, and (if the
expected read-only tools exist) dumps a sample account + positions response so
we can reconcile `_RH_TOOLS` and every `# TODO-VERIFY` field in
execution/robinhood_broker.py against reality.

It NEVER places, cancels, or modifies an order. The only tools it calls are
list/get read operations.

Auth:
    The token is read from the environment / .env (ROBINHOOD_AUTH_TOKEN) — do
    NOT pass it on the command line or paste it anywhere it could be logged.

Usage:
    # put ROBINHOOD_AUTH_TOKEN=... in .env first, then:
    .venv/bin/python scripts/robinhood_probe.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings  # noqa: E402
from execution.robinhood_broker import _RH_TOOLS, _McpHttpClient  # noqa: E402


def _dump(label: str, obj: object) -> None:
    print(f"\n──────── {label} ────────")
    print(json.dumps(obj, indent=2, default=str)[:4000])


def main() -> int:
    s = Settings()
    token = s.robinhood_auth_token
    url = s.robinhood_mcp_url
    if not token:
        print("ERROR: ROBINHOOD_AUTH_TOKEN is empty. Add it to .env first.")
        return 2

    print(f"Connecting to Robinhood MCP: {url}")
    client = _McpHttpClient(url, token)

    # 1) Handshake + tool catalogue — this is what we reconcile _RH_TOOLS against.
    try:
        tools = client.list_tools()
    except Exception as exc:  # noqa: BLE001
        print(f"\nlist_tools() FAILED: {type(exc).__name__}: {exc}")
        print("Check: token validity, endpoint URL, and the Authorization header"
              " scheme in _McpHttpClient._headers (currently Bearer).")
        return 1

    print(f"\nServer advertises {len(tools)} tool(s):")
    for t in tools:
        name = t.get("name", "?")
        desc = (t.get("description") or "").strip().replace("\n", " ")[:100]
        print(f"  • {name:30s} {desc}")
        schema = t.get("inputSchema") or t.get("input_schema")
        if schema:
            props = (schema.get("properties") or {}).keys()
            required = schema.get("required") or []
            print(f"      args: {', '.join(props) or '(none)'}"
                  f"   required: {', '.join(required) or '(none)'}")

    # 2) Show our current assumptions so the mismatch is obvious.
    advertised = {t.get("name") for t in tools}
    print("\n──────── _RH_TOOLS reconciliation ────────")
    for role, assumed in _RH_TOOLS.items():
        mark = "OK" if assumed in advertised else "MISSING — fix _RH_TOOLS"
        print(f"  {role:16s} assumed={assumed!r:24s} [{mark}]")

    # 3) Read-only response-shape inspection (safe — no orders).
    for role in ("get_account", "list_positions"):
        tool = _RH_TOOLS.get(role)
        if tool in advertised:
            try:
                _dump(f"{role} raw response", client.call_tool(tool, {}))
            except Exception as exc:  # noqa: BLE001
                print(f"\n{role} call failed: {type(exc).__name__}: {exc}")

    client.close()
    print("\nDone. Update _RH_TOOLS names + the TODO-VERIFY field accessors in "
          "execution/robinhood_broker.py to match the output above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
