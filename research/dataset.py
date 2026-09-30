"""Build the meta-labeling dataset: every SMC setup (loose filters) + features + outcome.

Features are computed strictly from information available at the setup's creation time
(MSS candle close). Outcome = triple barrier: stop, 2R target, 24h time limit, net of fees.

    python -m research.dataset --out data/research/setups.pkl
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from smcbot.config import load_config
from smcbot.market_data import resample
from smcbot.strategy import find_setups, prepare
from smcbot.utils import index_ns, ts_ns
from research.download import OUT, UNIVERSE

TAKER, SLIP = 0.0005, 0.0002
HOLD_BARS = 288  # 24h of 5m bars
TP_R = 2.0


def loose_config():
    cfg = load_config()
    s = cfg.strategy
    s.require_htf_alignment = False
    s.require_killzone = False
    s.min_sweep_weight = 0
    s.min_score = 0
    s.min_room_rr = 0
    s.entry_mode = "market"
    return cfg


def load(base: str) -> dict:
    k = pd.read_pickle(OUT / f"{base}_5m.pkl")
    m = pd.read_pickle(OUT / f"{base}_metrics.pkl")
    f = pd.read_pickle(OUT / f"{base}_funding.pkl")
    return {"k": k, "m": m, "f": f}


def frames(k: pd.DataFrame) -> dict:
    ohlcv = k[["open", "high", "low", "close", "volume"]]
    return {"exec": ohlcv, "ltf": resample(ohlcv, "15m"), "mtf": resample(ohlcv, "1h"),
            "htf": resample(ohlcv, "4h"), "d1": resample(ohlcv, "1d")}


def asof(series: pd.Series, ts: pd.Timestamp):
    i = series.index.searchsorted(ts, side="right") - 1
    return series.iloc[i] if i >= 0 else np.nan


def simulate(k5: dict, s) -> dict:
    """Triple-barrier outcome on 5m bars after the MSS close."""
    ns, h, lo, c = k5["ns"], k5["h"], k5["l"], k5["c"]
    i0 = int(np.searchsorted(ns, ts_ns(s.created_at)))
    if i0 >= len(ns) - 2:
        return {}
    d = s.dir
    entry = s.entry * (1 + d * SLIP)
    R = abs(entry - s.sl)
    fee_r = entry * 2 * TAKER / R
    tp = entry + d * TP_R * R
    mfe, exit_px, why, held = 0.0, None, "time", 0
    for i in range(i0, min(i0 + HOLD_BARS, len(ns))):
        held = i - i0 + 1
        if (lo[i] <= s.sl) if d == 1 else (h[i] >= s.sl):
            exit_px, why = s.sl * (1 - d * SLIP), "sl"
            break
        if (h[i] >= tp) if d == 1 else (lo[i] <= tp):
            exit_px, why = tp, "tp"
            break
        mfe = max(mfe, d * ((h[i] if d == 1 else lo[i]) - entry) / R)
    if exit_px is None:
        exit_px = c[min(i0 + HOLD_BARS, len(ns)) - 1]
    r_gross = d * (exit_px - entry) / R
    return {"r_net": r_gross - fee_r, "r_gross": r_gross, "fee_r": fee_r, "exit": why, "held_bars": held,
            "mfe_r": max(mfe, r_gross)}


def features(s, P, ctx: dict) -> dict:
    """Extra (non-SMC) features, all as of s.created_at, signed so that + = supports the trade."""
    t, d = s.created_at, s.dir
    k, m, f = ctx["k"], ctx["m"], ctx["f"]
    out = {}
    # time
    out["hour_sin"] = np.sin(2 * np.pi * t.hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * t.hour / 24)
    out["weekend"] = int(t.dayofweek >= 5)
    # volatility regime: 1h ATR% vs its own 30-day history
    mtf = P.mtf
    atr_pct = (P.atr_mtf_on_ltf / P.ltf["close"].to_numpy())
    i = int(np.searchsorted(P.ltf_close_ns, ts_ns(t), side="right")) - 1
    hist = atr_pct[max(0, i - 30 * 96):i + 1]
    out["atr_pct"] = float(atr_pct[i] * 100)
    out["atr_pctile"] = float((hist < atr_pct[i]).mean()) if len(hist) > 10 else np.nan
    # momentum over several horizons (signed)
    closes = ctx["close5"]
    j = int(closes.index.searchsorted(t, side="right")) - 1
    px = closes.iloc[j]
    for name, bars in (("ret_4h", 48), ("ret_24h", 288), ("ret_7d", 2016)):
        out[name] = d * (px / closes.iloc[max(0, j - bars)] - 1) * 100
    d1 = ctx["d1"]
    ema = d1["close"].ewm(span=20, adjust=False).mean()
    di = int(d1.index.searchsorted(t - pd.Timedelta(days=1), side="right")) - 1  # last closed day
    out["d1_ema20_dist"] = d * (px / ema.iloc[di] - 1) * 100 if di >= 0 else np.nan
    # taker flow: last 1h and the displacement leg (sweep -> MSS)
    sweep_t = pd.Timestamp(s.features["sweep_time"])
    win = k[(k.index >= t - pd.Timedelta(hours=1)) & (k.index < t)]
    leg = k[(k.index >= sweep_t) & (k.index < t)]
    tb = lambda w: (w["taker_buy_volume"].sum() / w["volume"].sum()) if len(w) and w["volume"].sum() > 0 else np.nan
    out["taker_1h"] = d * (tb(win) - 0.5) * 100
    out["taker_leg"] = d * (tb(leg) - 0.5) * 100
    base_vol = k["quote_volume"].iloc[max(0, j - 2016):j].mean()
    out["vol_1h_ratio"] = (win["quote_volume"].sum() / (base_vol * 12)) if base_vol else np.nan
    # open interest (value known at each 5m snapshot)
    if not m.empty:
        oi = m["sum_open_interest"]
        oi_now = asof(oi, t)
        oi_pre = asof(oi, sweep_t - pd.Timedelta(minutes=30))
        oi_24h = asof(oi, t - pd.Timedelta(hours=24))
        out["oi_chg_sweep"] = (oi_now / oi_pre - 1) * 100 if oi_pre else np.nan   # unsigned: stops -> OI drop
        out["oi_chg_24h"] = (oi_now / oi_24h - 1) * 100 if oi_24h else np.nan
        ls = m["count_long_short_ratio"]
        ls_now = asof(ls, t)
        out["ls_ratio"] = d * np.log(ls_now) if ls_now and ls_now > 0 else np.nan  # + = crowd on our side
        tls = m["sum_toptrader_long_short_ratio"]
        tls_now = asof(tls, t)
        out["top_ls_ratio"] = d * np.log(tls_now) if tls_now and tls_now > 0 else np.nan
    # funding (last settled rate, signed: + = our side is paying = crowded)
    if not f.empty:
        fr = asof(f["funding"], t)
        out["funding"] = d * fr * 10000 if fr == fr else np.nan  # in bps
        hist_f = f["funding"][(f.index > t - pd.Timedelta(days=30)) & (f.index <= t)]
        out["funding_z"] = d * (fr - hist_f.mean()) / hist_f.std() if len(hist_f) > 10 and hist_f.std() > 0 else np.nan
    # BTC context (for alts): BTC 4h trend and 24h return in trade direction
    btc = ctx.get("btc")
    if btc is not None:
        bi = int(btc["close5"].index.searchsorted(t, side="right")) - 1
        bpx = btc["close5"].iloc[bi]
        out["btc_ret_24h"] = d * (bpx / btc["close5"].iloc[max(0, bi - 288)] - 1) * 100
        out["btc_htf_trend"] = d * asof(btc["htf_trend"], t)
    return out


def build_symbol(base: str) -> pd.DataFrame:
    cfg = loose_config()
    data = load(base)
    fr = frames(data["k"])
    P = prepare(fr, cfg)
    btc_ctx = None
    ref = None
    if base != "BTC":
        bdata = load("BTC")
        bfr = frames(bdata["k"])
        bP = prepare(bfr, cfg)
        ref = bP
        htf_close = bfr["htf"].index + pd.Timedelta(hours=4)
        from smcbot.smc.structure import market_structure
        btrend = pd.Series(market_structure(bfr["htf"], cfg.strategy.swing_length_htf)["trend"].to_numpy(),
                           index=htf_close)
        btc_ctx = {"close5": bdata["k"]["close"], "htf_trend": btrend}
    else:
        edata = load("ETH")
        ref = prepare(frames(edata["k"]), cfg)
    ctx = {"k": data["k"], "m": data["m"], "f": data["f"], "d1": fr["d1"], "close5": data["k"]["close"],
           "btc": btc_ctx}
    k5 = {"ns": index_ns(data["k"].index), "h": data["k"]["high"].to_numpy(float),
          "l": data["k"]["low"].to_numpy(float), "c": data["k"]["close"].to_numpy(float)}
    start = pd.Timestamp("2024-09-30", tz="UTC")
    rows = []
    for s in find_setups(base, P, cfg, ref, ts_ns(start)):
        out = simulate(k5, s)
        if not out:
            continue
        row = {"id": s.id, "symbol": base, "side": s.side, "created_at": s.created_at, "score": s.score,
               "poi": s.poi, "swept": "|".join(s.swept), "tp_r_setup": s.tp_r, "dol_rr": min(s.dol_rr, 10),
               **{k: v for k, v in s.features.items() if k not in ("sweep_time", "mss_time", "entry_mode", "dol_rr")},
               **features(s, P, ctx), **out}
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(OUT / "setups.pkl"))
    p.add_argument("--symbols", default=",".join(UNIVERSE))
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args()
    bases = a.symbols.split(",")
    with ProcessPoolExecutor(a.workers) as ex:
        parts = list(ex.map(build_symbol, bases))
    df = pd.concat([x for x in parts if len(x)], ignore_index=True)
    df.to_pickle(a.out)
    print(f"{len(df)} setup, {df.symbol.nunique()} coin -> {a.out}")
    print(df.groupby("symbol").size().to_string())


if __name__ == "__main__":
    main()
