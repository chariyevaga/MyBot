"""Position optimization: decides stop moves, take-profit extensions and early exits.

Pure logic: it returns actions and never talks to the exchange, so the live engine and the
backtester share exactly the same rules.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategy import Prepared
from .trade import Trade
from .utils import ts_ns


@dataclass
class Action:
    kind: str            # move_sl | set_tp | close
    price: float | None
    reason: str


@dataclass
class OptContext:
    now: pd.Timestamp
    price: float
    atr_ltf: float
    ltf_break_against: bool      # 15m BOS/CHoCH against the position since the fill
    ltf_break_with: bool         # 15m BOS/CHoCH in favour since the fill
    mtf_break_against: bool      # 1H BOS/CHoCH against since the fill
    trail_swing: float | None    # latest confirmed 15m swing (low for longs) formed after the fill
    news_minutes: float | None   # minutes until the next high-impact event


def build_context(P: Prepared, trade: Trade, now: pd.Timestamp, price: float,
                  news_minutes: float | None) -> OptContext:
    d = trade.dir
    filled_ns = ts_ns(trade.filled_ts)
    now_ns = ts_ns(now)

    def breaks(ms: pd.DataFrame, close_ns: np.ndarray) -> tuple[bool, bool]:
        a = int(np.searchsorted(close_ns, filled_ns, side="right"))
        b = int(np.searchsorted(close_ns, now_ns, side="right"))
        if b <= a:
            return False, False
        ev = ms["bos"].to_numpy()[a:b].astype(int) + ms["choch"].to_numpy()[a:b].astype(int)
        return bool((ev == -d).any()), bool((ev == d).any())

    l_against, l_with = breaks(P.ms_ltf, P.ltf_close_ns)
    m_against, _ = breaks(P.ms_mtf, P.mtf_close_ns)

    last = int(np.searchsorted(P.ltf_close_ns, now_ns, side="right")) - 1
    trail = None
    atr_l = float(P.atr_ltf[last]) if last >= 0 else float("nan")
    if last >= 0:
        col_p, col_i = ("sl_price", "sl_idx") if d == 1 else ("sh_price", "sh_idx")
        sw_idx = int(P.ms_ltf[col_i].iat[last])
        if sw_idx >= 0 and P.ltf_open_ns[sw_idx] >= filled_ns:
            trail = float(P.ms_ltf[col_p].iat[last])
    return OptContext(now, price, atr_l, l_against, l_with, m_against, trail, news_minutes)


def optimize(t: Trade, ctx: OptContext, oc, max_rr: float) -> list[Action]:
    d = t.dir
    R = t.risk_unit
    if R <= 0 or t.filled_ts is None:
        return []
    r_now = t.r_at(ctx.price)
    mfe = max(t.r_at(t.mfe_price) if t.mfe_price else r_now, r_now)
    age_min = (ctx.now - t.filled_ts).total_seconds() / 60.0
    tp_r = t.r_at(t.tp)

    # --- early exits (never during the first min_hold_minutes; SL/TP still work) ---
    if age_min >= oc.min_hold_minutes:
        if oc.max_hold_hours and age_min >= oc.max_hold_hours * 60:
            return [Action("close", None, f"Maksimum süre doldu ({oc.max_hold_hours} saat)")]
        if oc.structure_exit and ctx.ltf_break_against and r_now < oc.structure_exit_max_r:
            return [Action("close", None, f"15m yapı pozisyon aleyhine kırıldı ({r_now:+.2f}R)")]
        if oc.htf_flip_exit and ctx.mtf_break_against and r_now < oc.htf_flip_exit_max_r:
            return [Action("close", None, f"1H yapı aleyhe döndü ({r_now:+.2f}R)")]
        if oc.stale_after_hours and age_min >= oc.stale_after_hours * 60 and mfe < oc.stale_max_mfe_r and r_now <= 0:
            return [Action("close", None, f"İşlem {oc.stale_after_hours} saatte çalışmadı (zaman stopu)")]

    # --- stop management: only ever tighten ---
    best = t.sl
    why: list[str] = []

    def tighten(candidate: float, reason: str) -> None:
        nonlocal best
        if d * (candidate - best) > 0:
            best = candidate
            why.append(reason)

    if oc.breakeven_at_r and mfe >= oc.breakeven_at_r:
        tighten(t.price_at_r(oc.breakeven_buffer_r), f"Break-even (+{oc.breakeven_at_r:g}R görüldü)")
    for trig, lock in oc.lock_steps:
        if mfe >= trig:
            tighten(t.price_at_r(lock), f"Kâr kilidi +{lock:g}R ({trig:g}R görüldü)")
    if oc.trail_structure and mfe >= oc.trail_after_r and ctx.trail_swing is not None and np.isfinite(ctx.atr_ltf):
        tighten(ctx.trail_swing - d * oc.trail_buffer_atr * ctx.atr_ltf, "Yapısal trailing (15m swing)")
    news_soon = ctx.news_minutes is not None and ctx.news_minutes <= oc.news_protect_minutes
    if news_soon and r_now >= oc.news_protect_min_r:
        tighten(t.price_at_r(oc.breakeven_buffer_r), f"Haber koruması ({ctx.news_minutes:.0f} dk sonra haber)")

    actions: list[Action] = []
    if why and abs(best - t.sl) >= oc.min_sl_step_r * R:
        if d * (ctx.price - best) <= 0:
            return [Action("close", None, "Yeni stop fiyatın gerisinde kaldı, kâr kilitlenerek çıkış")]
        actions.append(Action("move_sl", float(best), " + ".join(why)))

    # --- take-profit extension (never beyond max_rr) ---
    news_hour = ctx.news_minutes is not None and ctx.news_minutes <= 60
    if (oc.extend_tp and not t.tp_extended and tp_r < max_rr - 1e-9 and mfe >= oc.extend_trigger_r
            and ctx.ltf_break_with and not ctx.mtf_break_against and not news_hour):
        beyond = [t.r_at(x) for x in t.targets if t.r_at(x) > max(tp_r, mfe) + 1e-9]
        target = max_rr
        if beyond and min(beyond) < max_rr:
            target = min(beyond) - 0.05  # front-run the liquidity pool
        if target >= tp_r + 0.25:
            actions.append(Action("set_tp", float(t.price_at_r(target)),
                                  f"TP uzatıldı {tp_r:.1f}R → {target:.1f}R (momentum + likidite hedefi)"))
    return actions
