import pandas as pd
import pytest

from smcbot.optimizer import OptContext, optimize
from smcbot.trade import Trade

T0 = pd.Timestamp("2026-09-30 08:00", tz="UTC")


def make_trade(**kw):
    base = dict(id="T", symbol="BTC/USDT:USDT", side="long", status="open", setup_id="S", score=70, reasons=[],
                poi="FVG", swept=[], entry=100.0, sl=99.0, tp=102.0, initial_sl=99.0, initial_tp=102.0, tp_r=2.0,
                qty=1.0, risk_pct=2.0, risk_usd=20.0, leverage=5, created_at=T0.isoformat(),
                expires_at=T0.isoformat(), cancel_price=102.0, fill_price=100.0, filled_qty=1.0,
                filled_at=T0.isoformat(), mfe_price=100.0, targets=[103.5])
    base.update(kw)
    return Trade(**base)


def ctx(minutes, price, **kw):
    base = dict(now=T0 + pd.Timedelta(minutes=minutes), price=price, atr_ltf=0.2, ltf_break_against=False,
                ltf_break_with=False, mtf_break_against=False, trail_swing=None, news_minutes=None)
    base.update(kw)
    return OptContext(**base)


@pytest.fixture
def oc(cfg):
    return cfg.optimizer


def test_nothing_before_one_r(oc):
    assert optimize(make_trade(mfe_price=100.8), ctx(30, 100.7), oc, 3.0) == []


def test_breakeven_after_one_r(oc):
    acts = optimize(make_trade(mfe_price=101.05), ctx(30, 100.9), oc, 3.0)
    assert acts[0].kind == "move_sl" and acts[0].price == pytest.approx(100.05)


def test_profit_lock_steps(oc):
    acts = optimize(make_trade(mfe_price=101.6), ctx(90, 101.5), oc, 3.0)
    assert acts[0].kind == "move_sl" and acts[0].price == pytest.approx(100.5)


def test_stop_never_loosens(oc):
    assert optimize(make_trade(sl=100.6, mfe_price=101.6), ctx(90, 101.5), oc, 3.0) == []


def test_close_when_new_stop_is_behind_price(oc):
    acts = optimize(make_trade(mfe_price=101.6), ctx(90, 100.3), oc, 3.0)
    assert acts[0].kind == "close"


def test_no_early_exit_inside_min_hold(oc):
    oc.structure_exit = True
    assert optimize(make_trade(), ctx(30, 99.6, ltf_break_against=True), oc, 3.0) == []
    acts = optimize(make_trade(), ctx(61, 99.6, ltf_break_against=True), oc, 3.0)
    assert acts and acts[0].kind == "close"


def test_max_hold_time(oc):
    acts = optimize(make_trade(), ctx(oc.max_hold_hours * 60 + 1, 100.2), oc, 3.0)
    assert acts[0].kind == "close"


def test_tp_extension_front_runs_liquidity_and_respects_max_rr(oc):
    oc.extend_tp = True
    # buy-side liquidity at 2.6R -> TP just in front of it
    acts = optimize(make_trade(mfe_price=101.7, targets=[102.6]), ctx(120, 101.65, ltf_break_with=True), oc, 3.0)
    tp = [a for a in acts if a.kind == "set_tp"][0]
    assert tp.price == pytest.approx(102.55)
    # liquidity far away -> capped at max_rr (3R)
    acts = optimize(make_trade(mfe_price=101.7, targets=[110.0]), ctx(120, 101.65, ltf_break_with=True), oc, 3.0)
    tp = [a for a in acts if a.kind == "set_tp"][0]
    assert tp.price == pytest.approx(103.0)


def test_news_protection(oc):
    acts = optimize(make_trade(mfe_price=100.7), ctx(30, 100.6, news_minutes=20), oc, 3.0)
    assert acts[0].kind == "move_sl" and acts[0].price == pytest.approx(100.05)


def test_short_side_mirrors(oc):
    t = make_trade(side="short", sl=101.0, tp=98.0, initial_sl=101.0, initial_tp=98.0, mfe_price=98.95)
    acts = optimize(t, ctx(30, 99.1), oc, 3.0)
    assert acts[0].kind == "move_sl" and acts[0].price == pytest.approx(99.95)
