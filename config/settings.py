"""Application settings loaded from environment variables / .env file.

All knobs live here. Import `settings` for the module-level singleton,
or construct `Settings()` directly in tests.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Broker selection ───────────────────────────────────────────────────────
    # "alpaca" (paper, default) or "robinhood" (live agentic MCP). Switching to
    # robinhood does NOT by itself place live orders — robinhood_live_enabled
    # gates that separately (defaults False → dry-run).
    broker_kind: str = "alpaca"

    # ── Alpaca (paper) ─────────────────────────────────────────────────────────
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True

    # ── Robinhood (agentic MCP — REAL MONEY) ───────────────────────────────────
    # robinhood_live_enabled is the live-trading safety gate. False = dry-run:
    # the adapter logs intended orders and sends nothing. Set True only after the
    # MCP tool schema is verified and a dry-run→tiny-live test has passed.
    robinhood_mcp_url: str = "https://agent.robinhood.com/mcp/trading"
    robinhood_auth_token: str = ""
    robinhood_live_enabled: bool = False

    # ── Market data source ─────────────────────────────────────────────────────
    # "alpaca" requires a paid SIP subscription; "yfinance" is free daily bars.
    market_data_source: str = "yfinance"

    # ── Anthropic ──────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""

    # ── Finnhub free tier ──────────────────────────────────────────────────────
    finnhub_api_key: str = ""

    # ── Alerting ───────────────────────────────────────────────────────────────
    ntfy_topic: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Risk / leverage ────────────────────────────────────────────────────────
    master_capability: Decimal = Decimal("1.0")
    override_key: str = ""

    # ── Behaviour flags ────────────────────────────────────────────────────────
    auto_approve: bool = True
    log_level: str = "INFO"

    # ── Budget ─────────────────────────────────────────────────────────────────
    # Plan 2c (T2.6): tightened from $0.95/day to $0.10/day after the
    # cache-prefix fix (T1.1), schedule trims (T1.2/T1.3), and signal-
    # fingerprint gating (T1.4) reshape the call profile. Full breakdown
    # in CLAUDE_CODE_PLAN_2C_HANDOFF.md and logs/plan_2c_followups.md.
    daily_spend_cap: Decimal = Field(default=Decimal("0.10"))

    # ── Storage paths ──────────────────────────────────────────────────────────
    data_dir: str = "data"
    logs_dir: str = "logs"

    # ── Reconciler ─────────────────────────────────────────────────────────────
    # Alpaca has a push WS stream for fills; reconciler is a safety net (60s ok).
    # Robinhood has NO push stream — every fill and terminal state waits on the
    # poll. Tighten aggressively for robinhood (balanced vs rate limits on the
    # new MCP endpoint). Owner can override via env if the tighter default
    # triggers limits.
    reconciler_interval_secs: int = 60
    reconciler_interval_robinhood_secs: int = 20
    reconciler_qty_tolerance: Decimal = Decimal("1")   # shares

    # ── Per-agent tracker ──────────────────────────────────────────────────────
    starting_equity: Decimal = Decimal("30000")   # per-agent sleeve starting equity ($30k × 3 = $90k deployed, $10k Manager reserve)


settings = Settings()
