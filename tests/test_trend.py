import numpy as np
import pandas as pd

from smcbot import trend as T
from smcbot.market_data import resample


def _trend_frames(df5):
    return resample(df5, "4h"), resample(df5, "1d")


def _long_walk(days=240, drift=0.00004, seed=3):
    rng = np.random.default_rng(seed)
    n = days * 288
    rets = rng.normal(drift, 0.002, n)
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[100], close[:-1]])
    spread = np.abs(rng.normal(0, 0.0012, n)) * close
    idx = pd.date_range("2025-01-01", periods=n, freq="5min", tz="UTC").as_unit("ns")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + spread,
                         "low": np.minimum(open_, close) - spread, "close": close,
                         "volume": rng.uniform(50, 150, n)}, index=idx)


def test_trend_signals_have_no_lookahead(cfg):
    tc = cfg.strategies.trend
    df5 = _long_walk()
    full = T.signals("X", T.analyze(*_trend_frames(df5), tc), tc)
    assert len(full) > 10
    cutoff = df5.index[int(len(df5) * 0.6)]
    trunc = T.signals("X", T.analyze(*_trend_frames(df5[df5.index < cutoff]), tc), tc)
    before = {s.id: (round(s.entry, 8), round(s.sl, 8)) for s in full if s.created_at <= cutoff}
    got = {s.id: (round(s.entry, 8), round(s.sl, 8)) for s in trunc if s.id in before}
    assert before and got == before


def test_signal_geometry_and_filter(cfg):
    tc = cfg.strategies.trend
    st = T.analyze(*_trend_frames(_long_walk()), tc)
    for s in T.signals("X", st, tc):
        i = int(np.searchsorted(st.close_ns, s.created_at.value))
        d = s.dir
        assert s.tp is None and s.strategy == "trend"
        assert d * (s.entry - s.sl) > 0
        assert abs(abs(s.entry - s.sl) - tc.atr_mult * st.atr[i]) < 1e-9
        assert st.daily_trend[i] == d                       # daily EMA filter respected
        assert (s.entry > st.upper[i]) if d == 1 else (s.entry < st.lower[i])


def test_trailing_stop_follows_best_close(cfg):
    tc = cfg.strategies.trend
    st = T.analyze(*_trend_frames(_long_walk()), tc)
    s = next(x for x in T.signals("X", st, tc) if x.side == "long")
    i = int(np.searchsorted(st.close_ns, s.created_at.value))
    assert T.trail_candidate(st, int(st.close_ns[i]), 1, tc, upto_ns=int(st.close_ns[i])) is None
    j = i + 5
    cand = T.trail_candidate(st, int(st.close_ns[i]), 1, tc, upto_ns=int(st.close_ns[j]))
    best = st.h4["close"].to_numpy()[i:j + 1].max()
    assert cand == best - tc.atr_mult * st.atr[j]
