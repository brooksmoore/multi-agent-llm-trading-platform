"""Connect to Robinhood's agentic MCP via the official MCP SDK OAuth flow.

This uses the SDK's OAuthClientProvider, which performs the FULL MCP
authorization handshake (401 discovery -> dynamic registration -> PKCE
authorize -> browser consent -> token exchange) exactly as Claude Desktop
does. Our earlier hand-rolled authorize URL skipped the discovery handshake
and never triggered Robinhood's consent screen.

Run once interactively:
    uv run python scripts/robinhood_mcp_connect.py

On success it:
  - opens a browser to Robinhood's consent screen
  - captures the redirect on localhost
  - persists tokens + client info to ~/.robinhood_mcp_tokens.json
  - calls get_accounts to prove the session works

After this, the bot can reuse the stored tokens (SDK refreshes them).
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
import urllib.parse
import webbrowser
from pathlib import Path

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

MCP_URL = "https://agent.robinhood.com/mcp/trading"
TOKENS_PATH = Path("~/.robinhood_mcp_tokens.json").expanduser()
CALLBACK_PORT = 4321
CALLBACK_PATH = "/callback"


class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens + client registration to a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict = {}
        if path.exists():
            self._data = json.loads(path.read_text())

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2, default=str))
        self._path.chmod(0o600)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._data.get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json")
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._data.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._data["client_info"] = info.model_dump(mode="json")
        self._save()


def _wait_for_callback() -> tuple[str, str | None]:
    """Block on a local HTTP server until the OAuth redirect arrives."""
    result: dict[str, str | None] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            print(f"  [callback] {self.path}")
            if "code" in params:
                result["code"] = params["code"][0]
                result["state"] = params.get("state", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Authorised. Return to the terminal.</h2>")
            else:
                result["code"] = None
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>No code received.</h2>")

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), Handler)
    server.handle_request()
    server.server_close()
    if not result.get("code"):
        raise RuntimeError("No authorization code received on callback.")
    return result["code"], result.get("state")


async def _redirect_handler(auth_url: str) -> None:
    print("\n" + "=" * 70)
    print("Opening browser for Robinhood consent. Approve the connection.")
    print("=" * 70)
    print(f"\n{auth_url}\n")
    webbrowser.open(auth_url)


async def _callback_handler() -> tuple[str, str | None]:
    # Run the blocking server in a thread so we don't stall the event loop.
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _wait_for_callback)


async def main() -> None:
    storage = FileTokenStorage(TOKENS_PATH)
    client_metadata = OAuthClientMetadata(
        client_name="Multi-Agent Asset Bot",
        redirect_uris=[f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope="internal",
    )
    oauth = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
    )

    print(f"Connecting to {MCP_URL} via MCP SDK OAuth flow...")
    async with streamablehttp_client(MCP_URL, auth=oauth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("\nMCP session initialized. Calling get_accounts...")
            result = await session.call_tool("get_accounts", {})
            print("\n--- get_accounts result ---")
            for block in result.content:
                text = getattr(block, "text", None)
                if text:
                    print(text[:800])
    print(f"\nSuccess. Tokens persisted to {TOKENS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
