"""MarketData Protocol, Bar/Quote types, AlpacaMarketData (live) and ReplayMarketData (backtest)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, cast

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import Bar as AlpacaBar
from alpaca.data.models.bars import BarSet
from alpaca.data.models.quotes import Quote as AlpacaQuote
from alpaca.data.models.snapshots import Snapshot
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame


class Timeframe(StrEnum):
    MINUTE = "1Min"
    HOUR = "1Hour"
    DAY = "1Day"


_tf_map: dict[Timeframe, TimeFrame] = {
    Timeframe.MINUTE: TimeFrame.Minute,
    Timeframe.HOUR: TimeFrame.Hour,
    Timeframe.DAY: TimeFrame.Day,
}


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")


class MarketData(Protocol):
    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> list[Bar]: ...

    def get_latest_bar(self, symbol: str) -> Bar | None: ...

    def get_latest_quote(self, symbol: str) -> Quote | None: ...

    def get_snapshots(self, symbols: list[str]) -> dict[str, Bar]: ...


class AlpacaMarketData:
    def __init__(self, api_key: str, secret_key: str) -> None:
        self._client = StockHistoricalDataClient(api_key, secret_key)

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> list[Bar]:
        result = cast(
            "BarSet",
            self._client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=_tf_map[timeframe],
                    start=start,
                    end=end,
                )
            ),
        )
        return [self._to_bar(symbol, ab) for ab in result.data.get(symbol, [])]

    def get_latest_bar(self, symbol: str) -> Bar | None:
        result = cast(
            "dict[str, AlpacaBar]",
            self._client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=symbol)),
        )
        return self._to_bar(symbol, result[symbol]) if symbol in result else None

    def get_latest_quote(self, symbol: str) -> Quote | None:
        result = cast(
            "dict[str, AlpacaQuote]",
            self._client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol)),
        )
        return self._to_quote(symbol, result[symbol]) if symbol in result else None

    def get_snapshots(self, symbols: list[str]) -> dict[str, Bar]:
        result = cast(
            "dict[str, Snapshot]",
            self._client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols)),
        )
        out: dict[str, Bar] = {}
        for sym, snap in result.items():
            if snap.daily_bar is not None:
                out[sym] = self._to_bar(sym, snap.daily_bar)
        return out

    @staticmethod
    def _to_bar(symbol: str, ab: AlpacaBar) -> Bar:
        return Bar(
            symbol=symbol,
            timestamp=ab.timestamp,
            open=Decimal(str(ab.open)),
            high=Decimal(str(ab.high)),
            low=Decimal(str(ab.low)),
            close=Decimal(str(ab.close)),
            volume=int(ab.volume),
            vwap=Decimal(str(ab.vwap)) if ab.vwap is not None else None,
        )

    @staticmethod
    def _to_quote(symbol: str, aq: AlpacaQuote) -> Quote:
        return Quote(
            symbol=symbol,
            timestamp=aq.timestamp,
            bid=Decimal(str(aq.bid_price)),
            ask=Decimal(str(aq.ask_price)),
            bid_size=int(aq.bid_size),
            ask_size=int(aq.ask_size),
        )


class ReplayMarketData:
    def __init__(
        self,
        bars: dict[str, list[Bar]],
        quotes: dict[str, Quote] | None = None,
    ) -> None:
        self._bars = bars
        self._quotes: dict[str, Quote] = quotes if quotes is not None else {}

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> list[Bar]:
        return [b for b in self._bars.get(symbol, []) if start <= b.timestamp <= end]

    def get_latest_bar(self, symbol: str) -> Bar | None:
        bars = self._bars.get(symbol, [])
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def get_snapshots(self, symbols: list[str]) -> dict[str, Bar]:
        out: dict[str, Bar] = {}
        for sym in symbols:
            bar = self.get_latest_bar(sym)
            if bar is not None:
                out[sym] = bar
        return out
