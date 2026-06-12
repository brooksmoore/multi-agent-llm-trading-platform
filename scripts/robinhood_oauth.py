"""One-time Robinhood OAuth 2.1 (PKCE) authorisation flow.

Run this once to authorise the bot against Robinhood's agentic MCP trading
server.  A browser window opens; you log in; the script catches the redirect,
exchanges the code for tokens, and writes them to ~/.robinhood_token.json.

Usage:
    uv run python scripts/robinhood_oauth.py

Output:
    ~/.robinhood_token.json   ← keep private, never commit

The bot reads this file at startup via execution/robinhood_token.TokenProvider
and refreshes automatically before expiry (~4 days).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

# ── OAuth endpoints (from /.well-known/oauth-authorization-server) ─────────────
REGISTRATION_ENDPOINT = "https://agent.robinhood.com/oauth/trading/register"
AUTH_ENDPOINT = "https://robinhood.com/oauth"
TOKEN_ENDPOINT = "https://api.robinhood.com/oauth2/token/"
RESOURCE = "https://agent.robinhood.com/mcp/trading"
SCOPE = "internal"

REDIRECT_PORT = 4321
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TOKEN_PATH = Path("~/.robinhood_token.json").expanduser()


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _register_client() -> str:
    """Dynamically register a public OAuth client; return client_id."""
    print("Registering OAuth client with Robinhood...")
    resp = requests.post(
        REGISTRATION_ENDPOINT,
        json={
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Client registration failed ({resp.status_code}): {resp.text}"
        )
    data = resp.json()
    client_id = data.get("client_id") or data.get("clientId")
    if not client_id:
        raise RuntimeError(f"No client_id in registration response: {data}")
    print(f"  client_id: {client_id}")
    return client_id


def _catch_auth_code() -> str:
    """Start a local HTTP server and block until the auth redirect arrives."""
    code_holder: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                code_holder.append(params["code"][0])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b"<h2>Authorised! You can close this tab and return to the terminal.</h2>"
                )
            elif "error" in params:
                error = params.get("error", ["unknown"])[0]
                desc = params.get("error_description", [""])[0]
                code_holder.append(f"ERROR:{error}:{desc}")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"<h2>Error: {error} — {desc}</h2>".encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_: object) -> None:  # silence request logs
            pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    server.timeout = 120  # 2 min to complete browser login
    print(f"  Waiting for redirect on {REDIRECT_URI} (120s timeout)...")
    while not code_holder:
        server.handle_request()
    server.server_close()
    code = code_holder[0]
    if code.startswith("ERROR:"):
        raise RuntimeError(f"Robinhood auth error: {code}")
    return code


def _exchange_code(client_id: str, code: str, verifier: str) -> dict:
    """Exchange auth code for access + refresh tokens."""
    print("Exchanging auth code for tokens...")
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed ({resp.status_code}): {resp.text}\n"
            "Robinhood's token endpoint has a known quirk — if this fails,\n"
            "retry once; the second attempt often succeeds with identical params."
        )
    return resp.json()


def main() -> None:
    # 1. Register a fresh client
    client_id = _register_client()

    # 2. Build PKCE pair + auth URL
    verifier, challenge = _pkce_pair()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    auth_url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)

    # 3. Open browser
    print(f"\nOpening browser for Robinhood login...\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # 4. Catch redirect
    code = _catch_auth_code()
    print(f"  Received auth code ({len(code)} chars)")

    # 5. Exchange for tokens
    token_resp = _exchange_code(client_id, code, verifier)

    access_token = token_resp.get("access_token", "")
    refresh_token = token_resp.get("refresh_token", "")
    expires_in = int(token_resp.get("expires_in", 86400))
    expires_at = time.time() + expires_in

    if not access_token:
        raise RuntimeError(f"No access_token in response: {token_resp}")
    if not refresh_token:
        print("WARNING: no refresh_token returned — you may need to re-auth after expiry.")

    # 6. Persist
    payload = {
        "client_id": client_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }
    TOKEN_PATH.write_text(json.dumps(payload, indent=2))
    TOKEN_PATH.chmod(0o600)

    print(f"\nTokens saved to {TOKEN_PATH}")
    print(f"Access token expires in {expires_in // 3600}h {(expires_in % 3600) // 60}m")
    print("\nDone. The bot will now start with:  uv run python app.py")


if __name__ == "__main__":
    main()
