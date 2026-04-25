"""Tests for data/market.py — Bar/Quote types, AlpacaMarketData, ReplayMarketData."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from data.market import AlpacaMarketData, Bar, Quote, ReplayMarketData

_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TS2 = datetime(2026, 1, 5, tzinfo=UTC)
_TS3 = datetime(2026, 1, 10, tzinfo=UTC)


def _make_alpaca_bar(
    symbol: str,
    ts: datetime,
    o: float,
    h: float,
    low: float,
    c: float,
    volume: float,
    vwap: float | None = None,
) -> MagicMock:
    m = MagicMock()
    m.symbol = symbol
    m.timestamp = ts
    m.open = o
    m.high = h
    m.low = low
    m.close = c
    m.volume = volume
    m.vwap = vwap
    return m


def _make_alpaca_quote(
    symbol: str,
    ts: datetime,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
) -> MagicMock:
    m = MagicMock()
    m.symbol = symbol
    m.timestamp = ts
    m.bid_price = bid
    m.ask_price = ask
    m.bid_size = bid_size
    m.ask_size = ask_size
    return m


def _make_replay_bars(symbol: str, n: int = 5) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timestamp=datetime(2026, 1, i + 1, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=500_000,
            vwap=Decimal("103"),
        )
        for i in range(n)
    ]


def _client_with_mock() -> tuple[AlpacaMarketData, MagicMock]:
    with patch("data.market.StockHistoricalDataClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        broker = AlpacaMarketData("key", "secret")
    broker._client = mock_client  # noqa: SLF001
    return broker, mock_client


def test_bar_mid_price() -> None:
    q = Quote(
        symbol="SPY",
        timestamp=_TS,
        bid=Decimal("99"),
        ask=Decimal("101"),
        bid_size=100,
        ask_size=200,
    )
    assert q.mid == Decimal("100")


def test_replay_get_bars_filtered_by_date() -> None:
    bars = _make_replay_bars("AAPL", 5)
    md = ReplayMarketData({"AAPL": bars})
    start = datetime(2026, 1, 2, tzinfo=UTC)
    end = datetime(2026, 1, 4, tzinfo=UTC)
    result = md.get_bars("AAPL", start, end)
    assert len(result) == 3
    assert all(start <= b.timestamp <= end for b in result)


def test_replay_get_bars_empty_symbol() -> None:
    md = ReplayMarketData({})
    result = md.get_bars("MISSING", _TS, _TS2)
    assert result == []


def test_replay_get_latest_bar() -> None:
    bars = _make_replay_bars("TSLA", 3)
    md = ReplayMarketData({"TSLA": bars})
    result = md.get_latest_bar("TSLA")
    assert result == bars[-1]


def test_replay_get_latest_bar_missing() -> None:
    md = ReplayMarketData({})
    assert md.get_latest_bar("NOPE") is None


def test_replay_get_latest_quote() -> None:
    q = Quote(
        symbol="SPY",
        timestamp=_TS,
        bid=Decimal("400"),
        ask=Decimal("401"),
        bid_size=50,
        ask_size=75,
    )
    md = ReplayMarketData({}, quotes={"SPY": q})
    assert md.get_latest_quote("SPY") == q


def test_replay_get_snapshots() -> None:
    bars_spy = _make_replay_bars("SPY", 3)
    bars_aapl = _make_replay_bars("AAPL", 2)
    md = ReplayMarketData({"SPY": bars_spy, "AAPL": bars_aapl})
    result = md.get_snapshots(["SPY", "AAPL", "MISSING"])
    assert set(result.keys()) == {"SPY", "AAPL"}
    assert result["SPY"] == bars_spy[-1]
    assert result["AAPL"] == bars_aapl[-1]


def test_alpaca_get_bars_translates_to_bar() -> None:
    broker, client = _client_with_mock()
    ab = _make_alpaca_bar("SPY", _TS, 440.0, 450.0, 435.0, 448.0, 1_000_000, 447.5)
    bar_set = MagicMock()
    bar_set.data = {"SPY": [ab]}
    client.get_stock_bars.return_value = bar_set
    bars = broker.get_bars("SPY", _TS, _TS2)
    assert len(bars) == 1
    b = bars[0]
    assert b.symbol == "SPY"
    assert b.timestamp == _TS
    assert b.open == Decimal("440.0")
    assert b.high == Decimal("450.0")
    assert b.low == Decimal("435.0")
    assert b.close == Decimal("448.0")
    assert b.volume == 1_000_000
    assert b.vwap == Decimal("447.5")


def test_alpaca_get_bars_empty() -> None:
    broker, client = _client_with_mock()
    bar_set = MagicMock()
    bar_set.data = {}
    client.get_stock_bars.return_value = bar_set
    bars = broker.get_bars("SPY", _TS, _TS2)
    assert bars == []


def test_alpaca_get_latest_bar_found() -> None:
    broker, client = _client_with_mock()
    ab = _make_alpaca_bar("AAPL", _TS, 170.0, 175.0, 168.0, 173.0, 800_000)
    client.get_stock_latest_bar.return_value = {"AAPL": ab}
    result = broker.get_latest_bar("AAPL")
    assert result is not None
    assert result.symbol == "AAPL"
    assert result.close == Decimal("173.0")
    assert result.vwap is None


def test_alpaca_get_latest_bar_missing() -> None:
    broker, client = _client_with_mock()
    client.get_stock_latest_bar.return_value = {}
    result = broker.get_latest_bar("AAPL")
    assert result is None


def test_alpaca_get_latest_quote() -> None:
    broker, client = _client_with_mock()
    aq = _make_alpaca_quote("SPY", _TS, 449.50, 449.75, 200.0, 150.0)
    client.get_stock_latest_quote.return_value = {"SPY": aq}
    result = broker.get_latest_quote("SPY")
    assert result is not None
    assert result.bid == Decimal("449.50")
    assert result.ask == Decimal("449.75")
    assert result.bid_size == 200
    assert result.ask_size == 150


def test_alpaca_get_latest_quote_missing() -> None:
    broker, client = _client_with_mock()
    client.get_stock_latest_quote.return_value = {}
    result = broker.get_latest_quote("SPY")
    assert result is None


def test_alpaca_get_snapshots() -> None:
    broker, client = _client_with_mock()
    ab = _make_alpaca_bar("MSFT", _TS, 300.0, 310.0, 298.0, 308.0, 600_000, 305.5)
    snap = MagicMock()
    snap.daily_bar = ab
    client.get_stock_snapshot.return_value = {"MSFT": snap}
    result = broker.get_snapshots(["MSFT"])
    assert "MSFT" in result
    bar = result["MSFT"]
    assert bar.open == Decimal("300.0")
    assert bar.close == Decimal("308.0")
    assert bar.vwap == Decimal("305.5")


def test_alpaca_get_snapshots_no_daily_bar() -> None:
    broker, client = _client_with_mock()
    snap = MagicMock()
    snap.daily_bar = None
    client.get_stock_snapshot.return_value = {"TSLA": snap}
    result = broker.get_snapshots(["TSLA"])
    assert result == {}
