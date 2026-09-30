from __future__ import annotations

import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from .utils import PANDAS_RULE, base_of, now_utc, tf_delta, tf_ms

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]


def rows_to_df(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="time"))
    df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True).dt.as_unit("ns")
    df.index.name = "time"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.astype(float)


def drop_unclosed(df: pd.DataFrame, tf: str, now: pd.Timestamp | None = None) -> pd.DataFrame:
    now = now or now_utc()
    return df[df.index + tf_delta(tf) <= now]


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample OHLCV to a higher timeframe, keeping only complete buckets."""
    rule = PANDAS_RULE[tf]
    agg = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    counts = df["close"].resample(rule, label="left", closed="left").count()
    if len(df.index) > 1:
        base = df.index[1] - df.index[0]
        expected = int(tf_delta(tf) / base) if base > pd.Timedelta(0) else 1
        agg = agg[counts >= expected]
    return agg.dropna()


class MarketData:
    """OHLCV access through a ccxt exchange instance."""

    def __init__(self, exchange: ccxt.Exchange, cache_dir: str = "data"):
        self.ex = exchange
        self.cache_dir = Path(cache_dir)

    def candles(self, symbol: str, tf: str, limit: int = 500) -> pd.DataFrame:
        """Most recent *closed* candles."""
        rows = self._retry(lambda: self.ex.fetch_ohlcv(symbol, tf, limit=min(limit + 1, 1500)))
        return drop_unclosed(rows_to_df(rows), tf)

    def candles_since(self, symbol: str, tf: str, since_ms: int, limit: int = 1000) -> pd.DataFrame:
        rows = self._retry(lambda: self.ex.fetch_ohlcv(symbol, tf, since=since_ms, limit=limit))
        return drop_unclosed(rows_to_df(rows), tf)

    def last_price(self, symbol: str) -> float:
        t = self._retry(lambda: self.ex.fetch_ticker(symbol))
        return float(t["last"])

    def history(self, symbol: str, tf: str, start: pd.Timestamp, end: pd.Timestamp,
                use_cache: bool = True) -> pd.DataFrame:
        """Paginated history with an on-disk cache (used by the backtester)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{base_of(symbol)}_{tf}.csv.gz"
        cached = pd.DataFrame()
        if use_cache and path.exists():
            cached = pd.read_csv(path, index_col=0)
            cached.index = pd.to_datetime(cached.index, utc=True).as_unit("ns")
            cached.index.name = "time"
        step = tf_ms(tf)
        need_from = int(start.value // 1_000_000)
        need_to = int(end.value // 1_000_000)
        parts = [cached] if not cached.empty else []
        if cached.empty or cached.index[0].value // 1_000_000 > need_from:
            upto = need_to if cached.empty else int(cached.index[0].value // 1_000_000)
            parts.append(self._fetch_range(symbol, tf, need_from, upto))
        if not cached.empty and cached.index[-1].value // 1_000_000 + step < need_to:
            parts.append(self._fetch_range(symbol, tf, int(cached.index[-1].value // 1_000_000) + step, need_to))
        fetched = len(parts) > (0 if cached.empty else 1)
        df = pd.concat([p for p in parts if not p.empty]) if parts else pd.DataFrame(columns=COLUMNS)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = drop_unclosed(df, tf)
        if use_cache and fetched and not df.empty:
            df.to_csv(path, compression="gzip")
        return df[(df.index >= start) & (df.index < end)]

    def _fetch_range(self, symbol: str, tf: str, since_ms: int, until_ms: int) -> pd.DataFrame:
        rows: list = []
        cursor = since_ms
        step = tf_ms(tf)
        while cursor < until_ms:
            batch = self._retry(lambda: self.ex.fetch_ohlcv(symbol, tf, since=cursor, limit=1500))
            if not batch:
                break
            rows.extend(batch)
            last = batch[-1][0]
            if last + step <= cursor:
                break
            cursor = last + step
        df = rows_to_df(rows)
        log.info("%s %s: %d mum indirildi", symbol, tf, len(df))
        return df

    @staticmethod
    def _retry(fn, attempts: int = 4):
        delay = 1.0
        for i in range(attempts):
            try:
                return fn()
            except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable) as e:
                if i == attempts - 1:
                    raise
                log.warning("Ağ hatası (%s), %.0fs sonra tekrar", type(e).__name__, delay)
                time.sleep(delay)
                delay *= 2
