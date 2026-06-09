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

    def get_bars_batch(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> dict[str, list[Bar]]: ...

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

    def get_bars_batch(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> dict[str, list[Bar]]:
        if not symbols:
            return {}
        result = cast(
            "BarSet",
            self._client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=list(symbols),
                    timeframe=_tf_map[timeframe],
                    start=start,
                    end=end,
                )
            ),
        )
        out: dict[str, list[Bar]] = {sym: [] for sym in symbols}
        for sym in symbols:
            out[sym] = [self._to_bar(sym, ab) for ab in result.data.get(sym, [])]
        return out

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


class YFinanceMarketData:
    """Free daily bars via yfinance.

    Notes:
    - Daily bars only. Minute/hour timeframes fall through to daily.
    - Crypto symbols are routed to Alpaca's CryptoHistoricalDataClient when
      Alpaca creds are supplied — yfinance's BTC-USD / ETH-USD endpoints have
      been intermittently unavailable. Crypto-symbol form coming in: BTCUSD,
      ETHUSD, SOLUSD (no slash). Alpaca crypto API expects "BTC/USD" form.
    - get_latest_quote returns a Quote with bid==ask==close (no live quote feed).
    """

    _CRYPTO_MAP = {
        "BTCUSD": "BTC-USD",
        "ETHUSD": "ETH-USD",
        "SOLUSD": "SOL-USD",
    }
    _ALPACA_CRYPTO_MAP = {
        "BTCUSD": "BTC/USD",
        "ETHUSD": "ETH/USD",
        "SOLUSD": "SOL/USD",
    }

    def __init__(
        self,
        alpaca_api_key: str | None = None,
        alpaca_secret_key: str | None = None,
    ) -> None:
        self._alpaca_crypto: object | None = None
        if alpaca_api_key and alpaca_secret_key:
            try:
                from alpaca.data.historical.crypto import (  # noqa: PLC0415
                    CryptoHistoricalDataClient,
                )
                self._alpaca_crypto = CryptoHistoricalDataClient(
                    alpaca_api_key, alpaca_secret_key
                )
            except Exception:
                self._alpaca_crypto = None

    @classmethod
    def _yf_symbol(cls, symbol: str) -> str:
        return cls._CRYPTO_MAP.get(symbol, symbol)

    @classmethod
    def _is_crypto(cls, symbol: str) -> bool:
        return symbol in cls._ALPACA_CRYPTO_MAP

    def _alpaca_crypto_bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[Bar]:
        """Fetch daily crypto bars from Alpaca; empty list if unavailable."""
        if self._alpaca_crypto is None:
            return []
        try:
            from alpaca.data.requests import CryptoBarsRequest  # noqa: PLC0415
            from alpaca.data.timeframe import TimeFrame  # noqa: PLC0415
            alpaca_sym = self._ALPACA_CRYPTO_MAP[symbol]
            req = CryptoBarsRequest(
                symbol_or_symbols=alpaca_sym,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            )
            result = self._alpaca_crypto.get_crypto_bars(req)  # type: ignore[attr-defined]
            data = result.data.get(alpaca_sym, [])
        except Exception:
            return []
        bars: list[Bar] = []
        for ab in data:
            try:
                bars.append(Bar(
                    symbol=symbol,
                    timestamp=ab.timestamp,
                    open=Decimal(str(ab.open)),
                    high=Decimal(str(ab.high)),
                    low=Decimal(str(ab.low)),
                    close=Decimal(str(ab.close)),
                    volume=int(ab.volume),
                ))
            except (AttributeError, ValueError, TypeError):
                continue
        return bars

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> list[Bar]:
        if self._is_crypto(symbol):
            bars = self._alpaca_crypto_bars(symbol, start, end)
            if bars:
                return bars
            # If Alpaca crypto unavailable, fall through to yfinance.

        import yfinance as yf  # noqa: PLC0415

        ticker = yf.Ticker(self._yf_symbol(symbol))
        df = ticker.history(start=start, end=end, interval="1d", auto_adjust=False)
        if df is None or df.empty:
            return []

        bars: list[Bar] = []
        for ts, row in df.iterrows():
            ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            try:
                bars.append(
                    Bar(
                        symbol=symbol,
                        timestamp=ts_dt,
                        open=Decimal(str(row["Open"])),
                        high=Decimal(str(row["High"])),
                        low=Decimal(str(row["Low"])),
                        close=Decimal(str(row["Close"])),
                        volume=int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return bars

    def get_bars_batch(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> dict[str, list[Bar]]:
        if not symbols:
            return {}
        out: dict[str, list[Bar]] = {sym: [] for sym in symbols}

        # Crypto: still per-symbol via Alpaca crypto client (CryptoBarsRequest
        # does accept a list, but the count is tiny so it's not worth a second
        # code path here).
        equity_syms: list[str] = []
        for sym in symbols:
            if self._is_crypto(sym):
                bars = self._alpaca_crypto_bars(sym, start, end)
                if bars:
                    out[sym] = bars
                else:
                    equity_syms.append(sym)  # fall through to yfinance batch
            else:
                equity_syms.append(sym)

        if not equity_syms:
            return out

        import yfinance as yf  # noqa: PLC0415

        yf_to_orig = {self._yf_symbol(s): s for s in equity_syms}
        df = yf.download(
            tickers=list(yf_to_orig.keys()),
            start=start,
            end=end,
            interval="1d",
            auto_adjust=False,
            group_by="ticker",
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            return out

        # yf.download returns a multi-index frame when given >1 ticker, or a
        # flat OHLCV frame for a single ticker.
        single_ticker = len(yf_to_orig) == 1
        for yf_sym, orig_sym in yf_to_orig.items():
            try:
                sub = df if single_ticker else df[yf_sym]
            except KeyError:
                continue
            if sub is None or sub.empty:
                continue
            bars: list[Bar] = []
            for ts, row in sub.iterrows():
                ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                try:
                    o, h, l, c, v = row["Open"], row["High"], row["Low"], row["Close"], row["Volume"]
                    if o != o or c != c:  # NaN guard
                        continue
                    bars.append(
                        Bar(
                            symbol=orig_sym,
                            timestamp=ts_dt,
                            open=Decimal(str(o)),
                            high=Decimal(str(h)),
                            low=Decimal(str(l)),
                            close=Decimal(str(c)),
                            volume=int(v) if v == v else 0,
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    continue
            if bars:
                out[orig_sym] = bars
        return out

    def get_latest_bar(self, symbol: str) -> Bar | None:
        if self._is_crypto(symbol):
            from datetime import UTC, timedelta  # noqa: PLC0415
            now = datetime.now(UTC)
            bars = self._alpaca_crypto_bars(symbol, now - timedelta(days=5), now)
            if bars:
                return bars[-1]
            # Fall through to yfinance if Alpaca didn't return data.

        import yfinance as yf  # noqa: PLC0415

        df = yf.Ticker(self._yf_symbol(symbol)).history(period="5d", interval="1d")
        if df is None or df.empty:
            return None
        ts = df.index[-1]
        ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        row = df.iloc[-1]
        return Bar(
            symbol=symbol,
            timestamp=ts_dt,
            open=Decimal(str(row["Open"])),
            high=Decimal(str(row["High"])),
            low=Decimal(str(row["Low"])),
            close=Decimal(str(row["Close"])),
            volume=int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        )

    def get_latest_quote(self, symbol: str) -> Quote | None:
        bar = self.get_latest_bar(symbol)
        if bar is None:
            return None
        return Quote(
            symbol=symbol,
            timestamp=bar.timestamp,
            bid=bar.close,
            ask=bar.close,
            bid_size=0,
            ask_size=0,
        )

    def get_snapshots(self, symbols: list[str]) -> dict[str, Bar]:
        out: dict[str, Bar] = {}
        for sym in symbols:
            bar = self.get_latest_bar(sym)
            if bar is not None:
                out[sym] = bar
        return out


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

    def get_bars_batch(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> dict[str, list[Bar]]:
        return {
            sym: [b for b in self._bars.get(sym, []) if start <= b.timestamp <= end]
            for sym in symbols
        }

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
