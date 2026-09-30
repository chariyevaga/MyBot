from __future__ import annotations

import math

import numpy as np
import pandas as pd

TF_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

PANDAS_RULE = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1D"}


def tf_ms(tf: str) -> int:
    return TF_MS[tf]


def tf_ns(tf: str) -> int:
    return TF_MS[tf] * 1_000_000


def tf_delta(tf: str) -> pd.Timedelta:
    return pd.Timedelta(milliseconds=TF_MS[tf])


def now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def index_ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Nanosecond epoch values of a DatetimeIndex, independent of its resolution."""
    return index.as_unit("ns").asi8


def ts_ns(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).as_unit("ns").value)


def to_iso(ts) -> str | None:
    if ts is None:
        return None
    return pd.Timestamp(ts).tz_convert("UTC").isoformat()


def from_iso(s) -> pd.Timestamp | None:
    if s is None or s == "":
        return None
    ts = pd.Timestamp(s)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def ccxt_symbol(base: str, quote: str = "USDT") -> str:
    return f"{base}/{quote}:{quote}"


def base_of(symbol: str) -> str:
    return symbol.split("/")[0]


def fmt_price(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.1f}"
    if ax >= 10:
        return f"{x:,.3f}"
    if ax >= 1:
        return f"{x:.4f}"
    return f"{x:.6f}"


def fmt_usd(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x:,.2f} USDT"


def floor_time(ts: pd.Timestamp, minutes: int) -> pd.Timestamp:
    return ts.floor(f"{minutes}min")
