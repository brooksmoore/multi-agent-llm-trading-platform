"""Core domain types for the multi-agent trading bot.

All types are immutable-by-convention dataclasses. The OMS and RiskGate
operate on these types; adapters translate to/from broker-specific formats.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

# ─── Identifiers ──────────────────────────────────────────────────────────────

IntentId = uuid.UUID
OrderId = uuid.UUID
FillId = uuid.UUID
LotId = uuid.UUID


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def normalize_symbol(symbol: str) -> str:
    """Canonical form for trading symbols.

    Crypto pairs arrive in two forms ("BTC/USD" from Alpaca's positions/fills
    API, "BTCUSD" from agent universes and submit endpoints). Without
    canonicalization, the same logical position appears under two keys —
    breaking dedup in the lots ledger and in agent `held` sets, which then
    re-emits buy intents for already-held names.

    Equity tickers contain no slash and pass through unchanged.
    """
    return symbol.replace("/", "") if "/" in symbol else symbol


# ─── Enumerations ─────────────────────────────────────────────────────────────


class AgentId(StrEnum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"
    MANAGER = "manager"


class Sleeve(StrEnum):
    EQUITY = "equity"
    CRYPTO = "crypto"
    OPTIONS = "options"


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"
    REBALANCE_TO = "rebalance_to"
    CLOSE = "close"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class OrderClass(StrEnum):
    SIMPLE = "simple"
    BRACKET = "bracket"
    OCO = "oco"
    OTO = "oto"
    MLEG = "mleg"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    OPG = "opg"
    CLS = "cls"
    IOC = "ioc"
    FOK = "fok"


class OrderState(StrEnum):
    PENDING = "pending"       # Created locally, not yet submitted
    SUBMITTED = "submitted"   # Sent to broker, awaiting acknowledgement
    ACCEPTED = "accepted"     # Broker confirmed receipt
    PARTIAL = "partial"       # Partially filled
    FILLED = "filled"         # Fully filled
    CANCELLED = "cancelled"   # Cancelled (by us or broker)
    REJECTED = "rejected"     # Rejected by broker or RiskGate
    EXPIRED = "expired"       # Time-in-force expired unfilled


class OrderEvent(StrEnum):
    SUBMIT = "submit"
    ACCEPT = "accept"
    PARTIAL_FILL = "partial_fill"
    FULL_FILL = "full_fill"
    CANCEL = "cancel"
    REJECT = "reject"
    EXPIRE = "expire"


class KillSwitchState(StrEnum):
    OK = "ok"
    DAILY_LOSS = "daily_loss"         # -2% intraday
    DRAWDOWN_HALVED = "drawdown_halved"     # -15% from peak → halve sizes
    DRAWDOWN_PAUSED = "drawdown_paused"     # -25% from peak → pause new entries
    DRAWDOWN_LIQUIDATE = "drawdown_liquidate"  # -33% from peak → liquidate
    RECONCILIATION_BREAK = "reconciliation_break"
    HEARTBEAT_MISSED = "heartbeat_missed"
    BUDGET_EXHAUSTED = "budget_exhausted"


class DrawdownBucket(StrEnum):
    NORMAL = "normal"    # < 5%
    YELLOW = "yellow"    # 5–10%
    ORANGE = "orange"    # 10–15%
    RED = "red"          # 15–25%
    FORCED_CASH = "forced_cash"  # > 25%


class VixBucket(StrEnum):
    VERY_LOW = "very_low"   # < 12  → 0.6×
    SWEET_SPOT = "sweet_spot"  # 12–18 → 1.0×
    ELEVATED = "elevated"   # 18–25 → 0.8×
    STRESS = "stress"       # 25–35 → 0.5×
    CRISIS = "crisis"       # > 35  → 0.25×


class LotMethod(StrEnum):
    FIFO = "fifo"
    LIFO = "lifo"


class NewsSource(StrEnum):
    EDGAR = "edgar"
    FINNHUB = "finnhub"
    RSS = "rss"
    YFINANCE = "yfinance"
    FRED = "fred"
    REDDIT = "reddit"


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    OPTION = "option"


# ─── Core domain objects ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Intent:
    """An agent's proposed position change. Never a dollar amount — always a weight."""

    id: IntentId
    agent_id: AgentId
    symbol: str
    action: Action
    target_weight: Decimal          # 0.0–1.0 of the agent's sleeve equity
    sleeve: Sleeve
    signal: str                     # ≤140 chars: which signal fired
    conviction: int                 # 1–10
    rationale: str                  # ≤280 chars: why
    timestamp: datetime
    regime_observation: str = ""    # optional context from agent
    requires_approval: bool = False  # overridden by AUTO_APPROVE flag
    legs: tuple[OptionLeg, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OptionLeg:
    """One leg of a multi-leg options order."""

    symbol: str       # OCC option symbol e.g. "SPY251219C00500000"
    side: OrderSide
    ratio_qty: int    # must satisfy GCD(all legs) == 1


@dataclass(frozen=True)
class Order:
    """An instruction sent (or to be sent) to the broker.

    Created by ExecutionPlanner from an Intent. Owns the FSM state.
    """

    id: OrderId
    intent_id: IntentId
    agent_id: AgentId
    symbol: str
    side: OrderSide
    qty: Decimal
    order_type: OrderType
    order_class: OrderClass
    time_in_force: TimeInForce
    state: OrderState
    created_at: datetime
    # Optional fields
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_percent: Decimal | None = None
    broker_order_id: str | None = None    # Alpaca's UUID string
    legs: tuple[OptionLeg, ...] = field(default_factory=tuple)
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    filled_qty: Decimal = Decimal("0")
    filled_avg_price: Decimal | None = None
    rejection_reason: str | None = None
    is_letf: bool = False    # leveraged ETF — triggers 5-day hold check
    letf_entry_date: date | None = None
    # Use dataclasses.replace(order, state=..., ...) to produce mutated copies.


@dataclass(frozen=True)
class Fill:
    """A confirmed execution from the broker."""

    id: FillId
    order_id: OrderId
    agent_id: AgentId
    symbol: str
    side: OrderSide
    qty: Decimal
    price: Decimal
    timestamp: datetime
    commission: Decimal = Decimal("0")
    is_partial: bool = False


@dataclass(frozen=True)
class Position:
    """Current open position for a single symbol, scoped to one agent."""

    agent_id: AgentId
    symbol: str
    qty: Decimal              # positive = long, negative = short (future)
    avg_entry_price: Decimal
    current_price: Decimal
    asset_class: AssetClass
    sleeve: Sleeve
    as_of: datetime

    @property
    def market_value(self) -> Decimal:
        return self.qty * self.current_price

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.current_price - self.avg_entry_price) * self.qty

    @property
    def unrealized_pnl_pct(self) -> Decimal:
        if self.avg_entry_price == Decimal("0"):
            return Decimal("0")
        return (self.current_price - self.avg_entry_price) / self.avg_entry_price


@dataclass(frozen=True)
class Lot:
    """A single tax lot — one purchase of shares.

    FIFO/LIFO consumption tracked here; required for wash-sale and tax-preference checks.
    """

    id: LotId
    agent_id: AgentId
    symbol: str
    qty: Decimal
    entry_price: Decimal
    entry_date: date
    entry_fill_id: FillId
    # Set when fully or partially consumed
    remaining_qty: Decimal = field(default_factory=lambda: Decimal("0"))
    exit_fill_id: FillId | None = None
    exit_date: date | None = None
    exit_price: Decimal | None = None
    is_closed: bool = False

    def __post_init__(self) -> None:
        # remaining_qty defaults to qty on creation
        if self.remaining_qty == Decimal("0") and not self.is_closed:
            object.__setattr__(self, "remaining_qty", self.qty)

    @property
    def holding_days(self) -> int | None:
        if self.exit_date is None:
            return None
        return (self.exit_date - self.entry_date).days

    @property
    def is_long_term(self) -> bool:
        """True if holding period qualifies as long-term (>= 366 days)."""
        days = self.holding_days
        return days is not None and days >= 366

    @property
    def realized_pnl(self) -> Decimal | None:
        if self.exit_price is None:
            return None
        closed_qty = self.qty - self.remaining_qty
        return (self.exit_price - self.entry_price) * closed_qty


@dataclass(frozen=True)
class NewsItem:
    """A single news event from any adapter."""

    source: NewsSource
    headline: str
    url: str
    published_at: datetime
    symbols: tuple[str, ...]   # tickers mentioned
    summary: str | None = None
    sentiment: float | None = None  # –1.0 to +1.0, if available
    body: str | None = None         # full text (Opus deep-dives only)


@dataclass(frozen=True)
class AgentMemo:
    """One LLM call + its context, stored for calibration and audit."""

    id: uuid.UUID
    agent_id: AgentId
    call_type: str           # e.g. "morning_brief", "deep_dive", "news_scan"
    model: str               # e.g. "claude-haiku-4-5"
    timestamp: datetime
    cached_tokens: int
    new_input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    prompt_hash: str         # SHA-256 of the full prompt (for dedup detection)
    response_json: str       # raw JSON string from the model
    intents_emitted: int     # how many intents came from this call
    # Set after the fact for calibration
    realized_outcome: str | None = None  # "win" | "loss" | "flat" | None


@dataclass
class AgentState:
    """Mutable snapshot of an agent's current operational state.

    Not frozen — updated in place by the main loop.
    """

    agent_id: AgentId
    sleeve_equity: Decimal          # current sleeve NAV
    sleeve_peak_equity: Decimal     # rolling 30-day high (for drawdown ladder)
    drawdown_bucket: DrawdownBucket
    drawdown_bucket_entry_date: date | None  # for recovery rule
    consecutive_losses: int         # benching trigger at 5
    is_benched: bool
    bench_until: datetime | None
    day_trade_count: int            # PDT counter, resets in 5-day window
    orders_today: int               # per-agent daily order cap
    last_memo_id: uuid.UUID | None

    @property
    def drawdown_pct(self) -> Decimal:
        if self.sleeve_peak_equity == Decimal("0"):
            return Decimal("0")
        return (self.sleeve_peak_equity - self.sleeve_equity) / self.sleeve_peak_equity


@dataclass(frozen=True)
class MarketSnapshot:
    """Minimal price + vol data the ExecutionPlanner needs to size an order."""

    current_prices: dict[str, Decimal]       # symbol → last mark price
    realized_vol_30d: dict[str, Decimal]     # symbol → annualized 30-day realized vol
    vix_bucket: VixBucket
    timestamp: datetime
