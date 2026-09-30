"""4H trend following (Donchian breakout + ATR chandelier stop) on the research universe.

Costs: taker fee + slippage on both sides and funding payments while the position is open.

    python -m research.trend --n 20 --atr-mult 3 --max-hold-hours 0
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from smcbot.indicators import atr
from smcbot.market_data import resample
from research.download import OUT, UNIVERSE

TAKER, SLIP = 0.0005, 0.0002
Y1 = (pd.Timestamp("2024-09-30", tz="UTC"), pd.Timestamp("2025-09-30", tz="UTC"))
Y2 = (pd.Timestamp("2025-09-30", tz="UTC"), pd.Timestamp("2026-09-30", tz="UTC"))


def trades_for(base: str, n: int, atr_mult: float, max_hold_h: float, trend_filter: bool,
               long_only: bool) -> list[dict]:
    k = pd.read_pickle(OUT / f"{base}_5m.pkl")
    fund = pd.read_pickle(OUT / f"{base}_funding.pkl")["funding"]
    h4 = resample(k[["open", "high", "low", "close", "volume"]], "4h")
    d1 = resample(k[["open", "high", "low", "close", "volume"]], "1d")
    a = atr(h4, 14).to_numpy()
    o, h, lo, c = (h4[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    upper = h4["high"].rolling(n).max().shift(1).to_numpy()
    lower = h4["low"].rolling(n).min().shift(1).to_numpy()
    # daily trend filter: close vs EMA50 of *closed* daily candles
    ema = d1["close"].ewm(span=50, adjust=False).mean()
    d_close_time = d1.index + pd.Timedelta(days=1)
    h4_close_time = h4.index + pd.Timedelta(hours=4)
    di = np.searchsorted(d_close_time.asi8, h4_close_time.asi8, side="right") - 1
    d_trend = np.where(di >= 0, np.sign(d1["close"].to_numpy()[np.clip(di, 0, None)]
                                        - ema.to_numpy()[np.clip(di, 0, None)]), 0)
    times = h4_close_time
    out = []
    i = n + 15
    N = len(c)
    while i < N - 1:
        d = 0
        if c[i] > upper[i]:
            d = 1
        elif c[i] < lower[i] and not long_only:
            d = -1
        if d == 0 or (trend_filter and d_trend[i] != d) or not np.isfinite(a[i]):
            i += 1
            continue
        entry = c[i] * (1 + d * SLIP)
        stop = entry - d * atr_mult * a[i]
        R = abs(entry - stop)
        best = c[i]
        t_in = times[i]
        exit_px, why = None, "stop"
        j = i + 1
        while j < N:
            if (lo[j] <= stop) if d == 1 else (h[j] >= stop):
                exit_px = (min(o[j], stop) if d == 1 else max(o[j], stop)) * (1 - d * SLIP)
                break
            if max_hold_h and (times[j] - t_in).total_seconds() >= max_hold_h * 3600:
                exit_px, why = c[j] * (1 - d * SLIP), "time"
                break
            best = max(best, c[j]) if d == 1 else min(best, c[j])
            new_stop = best - d * atr_mult * a[j]
            stop = max(stop, new_stop) if d == 1 else min(stop, new_stop)
            j += 1
        if exit_px is None:
            exit_px, why = c[-1], "end"
            j = N - 1
        t_out = times[j]
        f = fund[(fund.index > t_in) & (fund.index <= t_out)].sum()
        gross = d * (exit_px - entry)
        cost = entry * TAKER + exit_px * TAKER + d * f * entry  # longs pay positive funding
        out.append({"symbol": base, "side": d, "t_in": t_in, "t_out": t_out, "r_net": (gross - cost) / R,
                    "r_gross": gross / R, "funding_r": d * f * entry / R, "hold_h": (t_out - t_in).total_seconds() / 3600,
                    "exit": why, "stop_pct": R / entry * 100})
        i = j + 1
    return out


def portfolio(tr: pd.DataFrame, risk_pct: float, start: pd.Timestamp, end: pd.Timestamp, max_pos: int) -> dict:
    """Fixed-fractional equity curve with a concurrency limit (trades taken in time order)."""
    tr = tr[(tr.t_in >= start) & (tr.t_in < end)].sort_values("t_in")
    eq, peak, dd = 1.0, 1.0, 0.0
    open_until: list[pd.Timestamp] = []
    taken = []
    events = []
    for _, t in tr.iterrows():
        open_until = [x for x in open_until if x > t.t_in]
        if len(open_until) >= max_pos:
            continue
        open_until.append(t.t_out)
        taken.append(t)
        events.append((t.t_out, t.r_net))
    for _, r in sorted(events, key=lambda x: x[0]):
        eq *= 1 + risk_pct / 100 * r
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    tk = pd.DataFrame(taken)
    return {"trades": len(tk), "return_pct": round((eq - 1) * 100, 1), "max_dd_pct": round(dd * 100, 1),
            "avg_r_net": round(tk.r_net.mean(), 3) if len(tk) else None,
            "win_pct": round((tk.r_net > 0).mean() * 100, 1) if len(tk) else None,
            "avg_hold_h": round(tk.hold_h.mean(), 1) if len(tk) else None}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--atr-mult", type=float, default=3.0)
    p.add_argument("--max-hold-hours", type=float, default=0)
    p.add_argument("--no-trend-filter", action="store_true")
    p.add_argument("--long-only", action="store_true")
    p.add_argument("--risk", type=float, default=1.0)
    p.add_argument("--max-pos", type=int, default=6)
    p.add_argument("--symbols", default=",".join(UNIVERSE))
    p.add_argument("--save", default=None)
    a = p.parse_args()
    rows = []
    for b in a.symbols.split(","):
        rows += trades_for(b, a.n, a.atr_mult, a.max_hold_hours, not a.no_trend_filter, a.long_only)
    tr = pd.DataFrame(rows)
    if a.save:
        tr.to_pickle(a.save)
    for name, (s, e) in (("Y1 2024-25", Y1), ("Y2 2025-26", Y2)):
        sub = tr[(tr.t_in >= s) & (tr.t_in < e)]
        print(f"{name}: all-trades n={len(sub)} avgR={sub.r_net.mean():+.3f} win={(sub.r_net > 0).mean() * 100:.0f}% "
              f"fundingR={sub.funding_r.mean():+.3f} | portfolio {portfolio(tr, a.risk, s, e, a.max_pos)}")


if __name__ == "__main__":
    main()
