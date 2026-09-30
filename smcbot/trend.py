"""4H trend following: Donchian breakout, daily EMA filter, ATR chandelier (trailing) stop.

Entry  : a 4H candle closes above the highest high of the previous ``donchian_bars`` candles
         (below the lowest low for shorts) while the last closed daily candle is on the same
         side of its EMA(``daily_ema``).
Stop   : entry - ``atr_mult`` x ATR(4H); afterwards trailed at best close since entry -
         ``atr_mult`` x ATR (only ever tightened). No take-profit: trends are ridden until the
         trailing stop is hit, positions typically stay open for several days.

Research: docs/RESEARCH.md ("Trend takibi"), research/trend.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import atr
from .strategy import Setup
from .utils import index_ns, to_iso

H4 = pd.Timedelta(hours=4)
D1 = pd.Timedelta(days=1)


@dataclass
class TrendState:
    """Per-bar indicator arrays of one symbol (index = 4H candle open time)."""

    h4: pd.DataFrame
    close_ns: np.ndarray
    upper: np.ndarray
    lower: np.ndarray
    atr: np.ndarray
    daily_trend: np.ndarray


def analyze(h4: pd.DataFrame, d1: pd.DataFrame, tc) -> TrendState:
    upper = h4["high"].rolling(tc.donchian_bars).max().shift(1).to_numpy()
    lower = h4["low"].rolling(tc.donchian_bars).min().shift(1).to_numpy()
    a = atr(h4, tc.atr_period).to_numpy()
    close_ns = index_ns(h4.index) + H4.value
    # daily trend from *closed* daily candles only
    ema = d1["close"].ewm(span=tc.daily_ema, adjust=False).mean().to_numpy()
    d_close_ns = index_ns(d1.index) + D1.value
    di = np.searchsorted(d_close_ns, close_ns, side="right") - 1
    ok = di >= 0
    dtrend = np.zeros(len(h4))
    dtrend[ok] = np.sign(d1["close"].to_numpy()[di[ok]] - ema[di[ok]])
    return TrendState(h4, close_ns, upper, lower, a, dtrend)


def signal_at(symbol: str, st: TrendState, i: int, tc) -> Setup | None:
    """Breakout signal on the (closed) 4H candle ``i``."""
    c = float(st.h4["close"].iat[i])
    a = st.atr[i]
    if i < tc.donchian_bars + tc.atr_period or not np.isfinite(a) or a <= 0:
        return None
    d = 0
    if np.isfinite(st.upper[i]) and c > st.upper[i]:
        d = 1
    elif tc.allow_short and np.isfinite(st.lower[i]) and c < st.lower[i]:
        d = -1
    if d == 0 or st.daily_trend[i] != d:
        return None
    created = pd.Timestamp(int(st.close_ns[i]), tz="UTC")
    stop = c - d * tc.atr_mult * a
    side = "long" if d == 1 else "short"
    level = st.upper[i] if d == 1 else st.lower[i]
    stop_pct = abs(c - stop) / c * 100
    return Setup(
        id=f"{symbol}|trend-{side}|{created:%Y%m%dT%H%M}",
        symbol=symbol, side=side, created_at=created,
        expires_at=created + pd.Timedelta(minutes=tc.max_signal_age_minutes),
        entry=c, sl=float(stop), tp=None, tp_r=0.0, score=0,
        reasons=[f"4H {tc.donchian_bars} mumluk {'zirve' if d == 1 else 'dip'} kırılımı",
                 f"Günlük kapanış EMA{tc.daily_ema} {'üstünde' if d == 1 else 'altında'}",
                 f"İz süren stop {tc.atr_mult:g}×ATR (%{stop_pct:.1f} uzakta), hedef yok"],
        poi="Donchian", zone_low=float(min(level, c)), zone_high=float(max(level, c)), sweep_price=float(level),
        swept=[], dol_rr=0.0, targets=[], atr_ltf=float(a), atr_mtf=float(a),
        features={"breakout_level": float(level), "atr_4h": float(a), "stop_pct": round(stop_pct, 3),
                  "breakout_atr": round(abs(c - level) / a, 3), "bar_time": to_iso(created)},
        strategy="trend",
    )


def signals(symbol: str, st: TrendState, tc, start_ns: int | None = None) -> list[Setup]:
    first = 0 if start_ns is None else int(np.searchsorted(st.close_ns, start_ns, side="left"))
    out = []
    for i in range(first, len(st.close_ns)):
        s = signal_at(symbol, st, i, tc)
        if s is not None:
            out.append(s)
    return out


def chandelier(best_close: float, atr_now: float, d: int, mult: float) -> float:
    return best_close - d * mult * atr_now


def trail_candidate(st: TrendState, entry_close_ns: int, d: int, tc, upto_ns: int | None = None) -> float | None:
    """Chandelier stop from all closed 4H candles since (and including) the entry candle."""
    b = len(st.close_ns) if upto_ns is None else int(np.searchsorted(st.close_ns, upto_ns, side="right"))
    a0 = int(np.searchsorted(st.close_ns, entry_close_ns, side="left"))
    if b - a0 < 2:  # nothing closed after the entry candle yet
        return None
    closes = st.h4["close"].to_numpy()[a0:b]
    best = closes.max() if d == 1 else closes.min()
    a = st.atr[b - 1]
    if not np.isfinite(a):
        return None
    return float(chandelier(best, a, d, tc.atr_mult))
