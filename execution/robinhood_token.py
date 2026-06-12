"""Robinhood OAuth token provider.

Reads a token file written by scripts/robinhood_oauth.py and returns a valid
access token, refreshing via the refresh_token grant before expiry.

Token file schema (JSON):
    {
        "client_id":     "<registered client id>",
        "access_token":  "<bearer token>",
        "refresh_token": "<refresh token>",
        "expires_at":    <unix timestamp float>
    }
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://api.robinhood.com/oauth2/token/"
RESOURCE = "https://agent.robinhood.com/mcp/trading"
REFRESH_BUFFER_SECS = 300  # refresh 5 min before actual expiry


class TokenProvider:
    """Thread-safe Robinhood access-token provider with automatic refresh."""

    def __init__(self, token_path: str | Path) -> None:
        self._path = Path(token_path).expanduser()
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(
                f"Robinhood token file not found: {self._path}\n"
                "Run  python scripts/robinhood_oauth.py  once to authorise."
            )
        self._data = json.loads(self._path.read_text())
        required = {"client_id", "access_token", "refresh_token", "expires_at"}
        missing = required - set(self._data)
        if missing:
            raise ValueError(f"Token file missing fields: {missing}")

    def get_token(self) -> str:
        """Return a valid Bearer token, refreshing if close to expiry."""
        with self._lock:
            if time.time() >= self._data["expires_at"] - REFRESH_BUFFER_SECS:
                self._refresh()
            return self._data["access_token"]

    def _refresh(self) -> None:
        log.info("Robinhood access token expiring soon — refreshing.")
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._data["refresh_token"],
                "client_id": self._data["client_id"],
                "resource": RESOURCE,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        self._data["access_token"] = body["access_token"]
        if "refresh_token" in body:
            self._data["refresh_token"] = body["refresh_token"]
        self._data["expires_at"] = time.time() + int(body.get("expires_in", 86400))
        self._path.write_text(json.dumps(self._data, indent=2))
        log.info("Robinhood token refreshed; next expiry in %ds.", body.get("expires_in", 86400))
