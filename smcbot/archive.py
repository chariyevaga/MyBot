"""Binance USDⓈ-M history from the public archive (data.binance.vision), cached on disk.

Monthly files are used for complete months, daily files for the current month. Everything
is cached under data/archive/ so each file is fetched once.
"""
from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um"
CACHE = Path("data/archive")
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
              "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
_session = requests.Session()


def _get_zip_csv(url: str, cache_file: Path) -> pd.DataFrame | None:
    if cache_file.exists():
        if cache_file.stat().st_size == 0:
            return None
        return pd.read_pickle(cache_file)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=60)
            break
        except requests.RequestException:
            if attempt == 2:
                raise
    if r.status_code == 404:
        cache_file.write_bytes(b"")  # remember "not available"
        return None
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split("\n", 1)[0]
    has_header = any(c.isalpha() for c in first.replace("e", "").replace("E", ""))
    df = pd.read_csv(io.StringIO(raw), header=0 if has_header else None)
    df.to_pickle(cache_file)
    return df


def _months(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return list(pd.date_range(start.to_period("M").to_timestamp(), end, freq="MS"))


def klines(symbol: str, interval: str, start: str, end: str, workers: int = 8) -> pd.DataFrame:
    """OHLCV + taker buy volume, index = open time (UTC)."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    last_full = (pd.Timestamp.now().to_period("M").to_timestamp() - pd.Timedelta(days=1)).to_period("M").to_timestamp()
    jobs = []
    for m in _months(s, e):
        if m <= last_full:
            tag = m.strftime("%Y-%m")
            jobs.append((f"{BASE}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{tag}.zip",
                         CACHE / "klines" / symbol / interval / f"{tag}.pkl"))
        else:
            for d in pd.date_range(m, min(e, pd.Timestamp.now().normalize() - pd.Timedelta(days=1)), freq="D"):
                tag = d.strftime("%Y-%m-%d")
                jobs.append((f"{BASE}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{tag}.zip",
                             CACHE / "klines" / symbol / interval / f"{tag}.pkl"))
    with ThreadPoolExecutor(workers) as ex:
        parts = [p for p in ex.map(lambda j: _get_zip_csv(*j), jobs) if p is not None and len(p)]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df.columns = KLINE_COLS[: len(df.columns)]
    df = df.apply(pd.to_numeric, errors="coerce").dropna(subset=["open_time"])
    df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True).dt.as_unit("ns")
    df.index.name = "time"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume", "count"]]
    return df[(df.index >= pd.Timestamp(start, tz="UTC")) & (df.index < pd.Timestamp(end, tz="UTC"))]


def metrics(symbol: str, start: str, end: str, workers: int = 16) -> pd.DataFrame:
    """5-minute open interest and long/short ratios (daily archive files)."""
    days = pd.date_range(pd.Timestamp(start), min(pd.Timestamp(end), pd.Timestamp.now().normalize()
                                                   - pd.Timedelta(days=1)), freq="D")
    jobs = [(f"{BASE}/daily/metrics/{symbol}/{symbol}-metrics-{d:%Y-%m-%d}.zip",
             CACHE / "metrics" / symbol / f"{d:%Y-%m-%d}.pkl") for d in days]
    with ThreadPoolExecutor(workers) as ex:
        parts = [p for p in ex.map(lambda j: _get_zip_csv(*j), jobs) if p is not None and len(p)]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df.index = pd.to_datetime(df["create_time"], utc=True).dt.as_unit("ns")
    df.index.name = "time"
    keep = ["sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
            "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
    df = df[[c for c in keep if c in df.columns]].apply(pd.to_numeric, errors="coerce")
    return df[~df.index.duplicated(keep="last")].sort_index()


def funding(symbol: str, start: str, end: str) -> pd.DataFrame:
    jobs = [(f"{BASE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{m:%Y-%m}.zip",
             CACHE / "funding" / symbol / f"{m:%Y-%m}.pkl") for m in _months(pd.Timestamp(start), pd.Timestamp(end))]
    parts = [p for p in (_get_zip_csv(*j) for j in jobs) if p is not None and len(p)]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df.index = pd.to_datetime(df["calc_time"].astype("int64"), unit="ms", utc=True).dt.as_unit("ns")
    df.index.name = "time"
    return df[["last_funding_rate"]].rename(columns={"last_funding_rate": "funding"}).astype(float).sort_index()
