"""Liquidity pools: where stop orders cluster.

Level sources (weight = importance used in the confluence score):
  PW  previous week high/low           15
  PD  previous day high/low            15
  EQ1 equal highs/lows on 1H           12
  SW1 1H swing high/low                10
  EQ15 equal highs/lows on 15m          9
  ASIA Asian session high/low           8
  SW15 15m swing high/low               5

Each level has an ``avail`` time (when it became known) and an ``expire`` time.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..utils import index_ns, tf_ns
from .structure import pivots

KIND_WEIGHT = {"PW": 15, "PD": 15, "EQ1": 12, "SW1": 10, "EQ15": 9, "ASIA": 8, "SW15": 5}
KIND_LABEL = {
    ("PW", True): "PWH", ("PW", False): "PWL",
    ("PD", True): "PDH", ("PD", False): "PDL",
    ("EQ1", True): "EQH(1H)", ("EQ1", False): "EQL(1H)",
    ("EQ15", True): "EQH(15m)", ("EQ15", False): "EQL(15m)",
    ("SW1", True): "1H swing high", ("SW1", False): "1H swing low",
    ("SW15", True): "15m swing high", ("SW15", False): "15m swing low",
    ("ASIA", True): "Asya high", ("ASIA", False): "Asya low",
}

HOUR_NS = 3_600_000_000_000
DAY_NS = 24 * HOUR_NS


@dataclass
class Levels:
    price: np.ndarray
    is_high: np.ndarray
    kind: np.ndarray
    weight: np.ndarray
    avail_ns: np.ndarray
    expire_ns: np.ndarray

    def __len__(self) -> int:
        return len(self.price)

    def flipped(self) -> "Levels":
        """Levels in the price-negated space used to scan short setups."""
        return Levels(-self.price, ~self.is_high, self.kind, self.weight, self.avail_ns, self.expire_ns)

    def label(self, k: int) -> str:
        return KIND_LABEL.get((str(self.kind[k]), bool(self.is_high[k])), str(self.kind[k]))


def _swing_levels(df: pd.DataFrame, length: int, tf: str, kind: str, life_ns: int, out: list) -> None:
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    open_ns = index_ns(df.index)
    n = len(df)
    ph, pl = pivots(high, low, length)
    step = tf_ns(tf)
    for arr, is_high, src in ((ph, True, high), (pl, False, low)):
        for i in np.flatnonzero(arr):
            c = i + length
            if c >= n:
                continue
            avail = int(open_ns[c] + step)
            out.append((src[i], is_high, kind, avail, avail + life_ns))


def _equal_levels(df: pd.DataFrame, length: int, tf: str, atr: np.ndarray, tol_atr: float,
                  lookback: int, kind: str, life_ns: int, out: list) -> None:
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    open_ns = index_ns(df.index)
    n = len(df)
    ph, pl = pivots(high, low, length)
    step = tf_ns(tf)
    for arr, is_high in ((ph, True), (pl, False)):
        src = high if is_high else low
        idxs = np.flatnonzero(arr)
        for a, k in enumerate(idxs):
            c = k + length
            if c >= n or not np.isfinite(atr[k]):
                continue
            tol = tol_atr * atr[k]
            for j in idxs[:a][::-1]:
                if k - j > lookback:
                    break
                if abs(src[k] - src[j]) > tol:
                    continue
                if is_high:
                    level = max(src[j], src[k])
                    between = high[j + 1:k]
                    intact = between.size == 0 or between.max() <= level
                else:
                    level = min(src[j], src[k])
                    between = low[j + 1:k]
                    intact = between.size == 0 or between.min() >= level
                if intact:
                    avail = int(open_ns[c] + step)
                    out.append((level, is_high, kind, avail, avail + life_ns))
                break


def _session_levels(mtf: pd.DataFrame, asia_end_hour: int, out: list) -> None:
    if mtf.empty:
        return
    idx = mtf.index
    # previous day
    day_key = idx.floor("1D")
    daily = mtf.groupby(day_key).agg(high=("high", "max"), low=("low", "min"), n=("close", "size"))
    for day, row in daily.iterrows():
        if row.n < 20:
            continue
        avail = int(pd.Timestamp(day).as_unit("ns").value + DAY_NS)
        out.append((row.high, True, "PD", avail, avail + 2 * DAY_NS))
        out.append((row.low, False, "PD", avail, avail + 2 * DAY_NS))
    # previous week (Monday 00:00 UTC start)
    week_key = idx.normalize() - pd.to_timedelta(idx.dayofweek, unit="D")
    weekly = mtf.groupby(week_key).agg(high=("high", "max"), low=("low", "min"), n=("close", "size"))
    for wk, row in weekly.iterrows():
        if row.n < 150:
            continue
        avail = int(pd.Timestamp(wk).as_unit("ns").value + 7 * DAY_NS)
        out.append((row.high, True, "PW", avail, avail + 7 * DAY_NS))
        out.append((row.low, False, "PW", avail, avail + 7 * DAY_NS))
    # Asian session range
    asia = mtf[idx.hour < asia_end_hour]
    if not asia.empty:
        ak = asia.index.floor("1D")
        ses = asia.groupby(ak).agg(high=("high", "max"), low=("low", "min"), n=("close", "size"))
        for day, row in ses.iterrows():
            if row.n < asia_end_hour:
                continue
            d0 = int(pd.Timestamp(day).as_unit("ns").value)
            avail = d0 + asia_end_hour * HOUR_NS
            out.append((row.high, True, "ASIA", avail, d0 + DAY_NS))
            out.append((row.low, False, "ASIA", avail, d0 + DAY_NS))


def build_levels(ltf: pd.DataFrame, mtf: pd.DataFrame, atr_ltf: np.ndarray, atr_mtf: np.ndarray,
                 liq_cfg, ltf_tf: str = "15m", mtf_tf: str = "1h") -> Levels:
    out: list = []
    _swing_levels(mtf, liq_cfg.swing_length_mtf, mtf_tf, "SW1", int(liq_cfg.swing_life_hours_mtf * HOUR_NS), out)
    _swing_levels(ltf, liq_cfg.swing_length_ltf, ltf_tf, "SW15", int(liq_cfg.swing_life_hours_ltf * HOUR_NS), out)
    _equal_levels(mtf, liq_cfg.swing_length_mtf, mtf_tf, atr_mtf, liq_cfg.eq_tolerance_atr,
                  liq_cfg.eq_lookback_mtf, "EQ1", int(liq_cfg.swing_life_hours_mtf * HOUR_NS), out)
    _equal_levels(ltf, 3, ltf_tf, atr_ltf, liq_cfg.eq_tolerance_atr,
                  liq_cfg.eq_lookback_ltf, "EQ15", int(liq_cfg.swing_life_hours_ltf * HOUR_NS), out)
    _session_levels(mtf, liq_cfg.asia_end_hour_utc, out)
    if not out:
        empty = np.array([])
        return Levels(empty.astype(float), empty.astype(bool), empty.astype(object),
                      empty.astype(int), empty.astype(np.int64), empty.astype(np.int64))
    price, is_high, kind, avail, expire = zip(*out)
    kind_arr = np.array(kind, dtype=object)
    return Levels(
        price=np.array(price, dtype=float),
        is_high=np.array(is_high, dtype=bool),
        kind=kind_arr,
        weight=np.array([KIND_WEIGHT[k] for k in kind_arr], dtype=int),
        avail_ns=np.array(avail, dtype=np.int64),
        expire_ns=np.array(expire, dtype=np.int64),
    )


def level_bounds(levels: Levels, ltf_open_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """First and one-past-last LTF bar index during which each level is active."""
    start = np.searchsorted(ltf_open_ns, levels.avail_ns, side="left")
    end = np.searchsorted(ltf_open_ns, levels.expire_ns, side="left")
    return start, end


def first_touch(levels: Levels, start: np.ndarray, end: np.ndarray,
                high: np.ndarray, low: np.ndarray) -> np.ndarray:
    """Index of the first LTF bar that trades through each level (len(high) if never)."""
    n = len(high)
    taken = np.full(len(levels), n, dtype=np.int64)
    for k in range(len(levels)):
        s, e = int(start[k]), int(end[k])
        if s >= e:
            continue
        if levels.is_high[k]:
            hit = high[s:e] > levels.price[k]
        else:
            hit = low[s:e] < levels.price[k]
        if hit.any():
            taken[k] = s + int(np.argmax(hit))
    return taken
