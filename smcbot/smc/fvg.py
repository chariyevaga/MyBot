"""Fair Value Gaps (3-candle imbalances)."""
from __future__ import annotations

import numpy as np


def bullish_fvg(high: np.ndarray, low: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bullish FVG indexed at the third candle ``i``: low[i] > high[i-2].

    Returns (bottom, top) arrays, NaN where there is no gap. The gap is known once candle ``i``
    has closed. Bearish FVGs are found by calling this on the price-negated series
    (see strategy._transform).
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    n = len(high)
    bottom = np.full(n, np.nan)
    top = np.full(n, np.nan)
    if n < 3:
        return bottom, top
    gap = low[2:] > high[:-2]
    bottom[2:] = np.where(gap, high[:-2], np.nan)
    top[2:] = np.where(gap, low[2:], np.nan)
    return bottom, top
