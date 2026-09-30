import pandas as pd

from smcbot.config import Cfg, _enforce_limits, _wrap
from smcbot.risk import RiskGuard, choose_leverage, position_size, risk_pct_for_score
from smcbot.storage.kv import MemoryKV

FEES = Cfg(maker=0.0002, taker=0.0005)


def test_position_size_loses_exactly_the_risk_at_stop():
    qty, risk_usd = position_size(1000, 100.0, 99.0, 2.0, FEES)
    assert risk_usd == 20
    loss = qty * (100 - 99) + qty * 100 * (FEES.maker + FEES.taker)
    assert abs(loss - 20) < 1e-9


def test_risk_tiers_are_capped(cfg):
    cfg.risk.tiers = _wrap([{"min_score": 80, "risk_pct": 9.0}, {"min_score": 0, "risk_pct": 2.0}])
    _enforce_limits(cfg)
    assert risk_pct_for_score(95, cfg.risk) == 4.0
    assert risk_pct_for_score(10, cfg.risk) == 2.0


def test_hard_limits(cfg):
    cfg.risk.max_risk_pct = 10
    cfg.risk.max_rr = 5
    _enforce_limits(cfg)
    assert cfg.risk.max_risk_pct == 4.0 and cfg.risk.max_rr == 3.0


def test_leverage_keeps_liquidation_beyond_stop():
    plan = choose_leverage(notional=8000, free_balance=1000, slots_left=1, sl_dist_frac=0.01, leverage_max=50)
    liq_distance = 1 / plan.leverage - 0.005
    assert 0.01 <= 0.7 * liq_distance
    assert plan.leverage >= 9 or plan.qty_scale < 1


def test_leverage_scales_down_when_margin_is_short():
    plan = choose_leverage(notional=100_000, free_balance=1000, slots_left=3, sl_dist_frac=0.01, leverage_max=20)
    assert plan.qty_scale < 1


def test_guard_daily_loss_and_streak(cfg):
    now = pd.Timestamp("2026-09-30 10:00", tz="UTC")
    g = RiskGuard(cfg.risk, MemoryKV())
    g.on_cycle(1000, now)
    assert g.can_open(now)[0]
    for _ in range(cfg.risk.max_consecutive_losses):
        g.register_close(-10, now)
    ok, why = g.can_open(now)
    assert not ok and "zarar" in why.lower()
    g.resume()
    assert g.can_open(now)[0]
    g.register_close(-cfg.risk.max_daily_loss_pct * 10, now)  # daily limit
    assert not g.can_open(now)[0]
    g.on_cycle(900, now + pd.Timedelta(days=1))  # new day resets the daily limit
    assert g.can_open(now + pd.Timedelta(days=1))[0]
