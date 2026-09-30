"""Swing points and market structure (BOS / CHoCH).

Everything here is causal: a pivot at bar ``i`` with ``length`` bars on each side is only
known at bar ``i + length``, and structure events are evaluated on candle closes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def pivots(high: np.ndarray, low: np.ndarray, length: int) -> tuple[np.ndarray, np.ndarray]:
    """Boolean arrays of pivot highs / pivot lows.

    Pivot high at i: high[i] is strictly above the previous ``length`` highs and not below the
    next ``length`` highs (so the first of two equal highs is the pivot).
    """
    h = pd.Series(np.asarray(high, dtype=float))
    lo = pd.Series(np.asarray(low, dtype=float))
    left_h = h.shift(1).rolling(length).max()
    right_h = h[::-1].shift(1).rolling(length).max()[::-1]
    left_l = lo.shift(1).rolling(length).min()
    right_l = lo[::-1].shift(1).rolling(length).min()[::-1]
    ph = ((h > left_h) & (h >= right_h)).to_numpy()
    pl = ((lo < left_l) & (lo <= right_l)).to_numpy()
    return ph, pl


def market_structure(df: pd.DataFrame, length: int) -> pd.DataFrame:
    """Swing-based market structure.

    Columns (value as known at the close of each bar):
      sh_price / sh_idx : latest confirmed swing high
      sl_price / sl_idx : latest confirmed swing low
      trend             : +1 bullish, -1 bearish, 0 undefined
      bos               : +1 / -1 break of structure in trend direction on this bar
      choch             : +1 / -1 change of character (trend flip) on this bar
    """
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)
    ph, pl = pivots(high, low, length)

    sh_price = np.full(n, np.nan)
    sl_price = np.full(n, np.nan)
    sh_idx = np.full(n, -1, dtype=np.int64)
    sl_idx = np.full(n, -1, dtype=np.int64)
    trend = np.zeros(n, dtype=np.int8)
    bos = np.zeros(n, dtype=np.int8)
    choch = np.zeros(n, dtype=np.int8)

    cur_sh, cur_sh_i, sh_live = np.nan, -1, False
    cur_sl, cur_sl_i, sl_live = np.nan, -1, False
    state = 0
    for t in range(n):
        i = t - length
        if i >= 0:
            if ph[i]:
                cur_sh, cur_sh_i, sh_live = high[i], i, True
            if pl[i]:
                cur_sl, cur_sl_i, sl_live = low[i], i, True
        c = close[t]
        if sh_live and c > cur_sh:
            if state == -1:
                choch[t] = 1
            else:
                bos[t] = 1
            state = 1
            sh_live = False
        elif sl_live and c < cur_sl:
            if state == 1:
                choch[t] = -1
            else:
                bos[t] = -1
            state = -1
            sl_live = False
        trend[t] = state
        sh_price[t], sh_idx[t] = cur_sh, cur_sh_i
        sl_price[t], sl_idx[t] = cur_sl, cur_sl_i

    return pd.DataFrame(
        {
            "sh_price": sh_price,
            "sh_idx": sh_idx,
            "sl_price": sl_price,
            "sl_idx": sl_idx,
            "trend": trend,
            "bos": bos,
            "choch": choch,
        },
        index=df.index,
    )
