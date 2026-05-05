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

    # ── Alpaca (paper) ─────────────────────────────────────────────────────────
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True

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
    daily_spend_cap: Decimal = Field(default=Decimal("0.95"))

    # ── Storage paths ──────────────────────────────────────────────────────────
    data_dir: str = "data"
    logs_dir: str = "logs"

    # ── Reconciler ─────────────────────────────────────────────────────────────
    reconciler_interval_secs: int = 60
    reconciler_qty_tolerance: Decimal = Decimal("1")   # shares

    # ── Per-agent tracker ──────────────────────────────────────────────────────
    starting_equity: Decimal = Decimal("30000")   # per-agent sleeve starting equity ($30k × 3 = $90k deployed, $10k Manager reserve)


settings = Settings()
