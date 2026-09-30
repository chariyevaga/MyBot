import numpy as np
import pandas as pd
import pytest

from smcbot.market_data import resample
from smcbot.smc.fvg import bullish_fvg
from smcbot.smc.structure import market_structure, pivots
from smcbot.strategy import find_setups, prepare


def test_pivot_detected_and_only_confirmed_later():
    high = np.array([1, 2, 3, 6, 3, 2, 1, 2, 3], dtype=float)
    low = high - 0.5
    ph, _ = pivots(high, low, 2)
    assert ph[3] and ph.sum() == 1
    # with the right-hand bars missing the pivot cannot be known yet
    ph_trunc, _ = pivots(high[:5], low[:5], 2)
    assert not ph_trunc[3]


def test_first_of_equal_highs_is_the_pivot():
    high = np.array([1, 2, 5, 5, 2, 1, 1], dtype=float)
    ph, _ = pivots(high, high - 1, 2)
    assert ph[2] and not ph[3]


def test_market_structure_bos_then_choch():
    # up-swing, pullback, break higher (BOS up), then collapse below the swing low (CHoCH down)
    closes = [10, 11, 12, 15, 13, 12, 11, 12, 13, 14, 16, 17, 16, 15, 12, 10, 9, 8]
    df = pd.DataFrame({"open": closes, "high": [c + 0.2 for c in closes],
                       "low": [c - 0.2 for c in closes], "close": closes},
                      index=pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC"))
    ms = market_structure(df, 2)
    assert (ms["bos"] == 1).any()
    assert (ms["choch"] == -1).any()
    first_up = int(np.flatnonzero(ms["bos"].to_numpy() == 1)[0])
    first_down = int(np.flatnonzero(ms["choch"].to_numpy() == -1)[0])
    assert first_up < first_down
    assert ms["trend"].iat[-1] == -1


def test_bullish_fvg():
    high = np.array([10, 12, 14, 15], dtype=float)
    low = np.array([9, 10.5, 11, 13], dtype=float)
    bottom, top = bullish_fvg(high, low)
    assert bottom[2] == 10 and top[2] == 11      # low[2]=11 > high[0]=10
    assert np.isnan(bottom[1])


def _frames(df5):
    return {"ltf": resample(df5, "15m"), "mtf": resample(df5, "1h"), "htf": resample(df5, "4h")}


def _loose(cfg):
    s = cfg.strategy
    s.require_htf_alignment = False
    s.require_killzone = False
    s.min_sweep_weight = 0
    s.min_score = 0
    s.min_room_rr = 0
    s.min_sl_pct = 0.2
    s.min_sl_atr_h1 = 0.3
    s.max_sl_atr_h1 = 10
    s.displacement_atr = 0.6
    return cfg


@pytest.mark.parametrize("entry_mode", ["market", "limit"])
def test_setups_have_no_lookahead(cfg, random_walk_5m, entry_mode):
    """Setups created before a cut-off must be identical whether or not later data exists."""
    cfg = _loose(cfg)
    cfg.strategy.entry_mode = entry_mode
    full = find_setups("TEST", prepare(_frames(random_walk_5m), cfg), cfg)
    assert len(full) > 5, "synthetic data should produce setups"
    cutoff = random_walk_5m.index[int(len(random_walk_5m) * 0.7)]
    trunc = find_setups("TEST", prepare(_frames(random_walk_5m[random_walk_5m.index < cutoff]), cfg), cfg)
    before = {s.id: (round(s.entry, 8), round(s.sl, 8), round(s.tp, 8), s.score)
              for s in full if s.created_at <= cutoff - pd.Timedelta(minutes=15)}
    got = {s.id: (round(s.entry, 8), round(s.sl, 8), round(s.tp, 8), s.score) for s in trunc if s.id in before}
    assert before and got == before


def test_setup_geometry(cfg, random_walk_5m):
    cfg = _loose(cfg)
    for s in find_setups("TEST", prepare(_frames(random_walk_5m), cfg), cfg):
        d = s.dir
        assert d * (s.entry - s.sl) > 0 and d * (s.tp - s.entry) > 0
        assert abs(abs(s.tp - s.entry) / abs(s.entry - s.sl) - s.tp_r) < 1e-6
        assert s.tp_r <= cfg.risk.max_rr
        assert abs(s.entry - s.sl) >= cfg.strategy.min_sl_pct / 100 * s.entry - 1e-9
