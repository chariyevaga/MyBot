"""Where does the trend strategy lose? Trade-level context features + bucket analysis.

    python -m research.trend_analysis --live-week        # last week's trades with context (Binance API)
    python -m research.trend_analysis                    # 2 years of backtest trades (archive), by feature bucket
"""
from __future__ import annotations

import argparse

import ccxt
import numpy as np
import pandas as pd

from smcbot.backtest import load_data, load_data_archive, run_trend_backtest
from smcbot.config import load_config
from smcbot.market_data import MarketData
from smcbot.utils import base_of, ccxt_symbol

SPLIT = pd.Timestamp("2025-09-30", tz="UTC")


def context(data: dict, trades: list, cfg) -> pd.DataFrame:
    btc = ccxt_symbol("BTC", cfg.quote)
    closes = {s: fr["htf"]["close"] for s, fr in data.items()}
    h4_close_idx = {s: fr["htf"].index + pd.Timedelta(hours=4) for s, fr in data.items()}
    d1 = {s: fr["d1"] for s, fr in data.items()}
    ema50 = {s: d["close"].ewm(span=50, adjust=False).mean() for s, d in d1.items()}
    ema200 = {s: d["close"].ewm(span=200, adjust=False).mean() for s, d in d1.items()}

    def last_closed_daily(s, t, series):
        idx = d1[s].index + pd.Timedelta(days=1)
        i = idx.searchsorted(t, side="right") - 1
        return series.iloc[i] if i >= 0 else np.nan

    def close_at(s, t, back=pd.Timedelta(0)):
        idx = h4_close_idx[s]
        i = idx.searchsorted(t - back, side="right") - 1
        return closes[s].iloc[i] if i >= 0 else np.nan

    df_tr = pd.DataFrame([{"symbol": t.symbol, "side": t.side, "t_in": pd.Timestamp(t.created_at),
                           "t_out": pd.Timestamp(t.closed_at)} for t in trades])
    signal_bars = df_tr.groupby("t_in").size()
    rows = []
    for t in trades:
        s, d, t0 = t.symbol, t.dir, pd.Timestamp(t.created_at)
        px = t.open_price
        e50 = last_closed_daily(s, t0, ema50[s])
        dclose = last_closed_daily(s, t0, d1[s]["close"])
        open_same = int(((df_tr.t_in < t0) & (df_tr.t_out > t0) & (df_tr.side == t.side)).sum())
        breadth = np.nanmean([np.sign(last_closed_daily(x, t0, d1[x]["close"]) - last_closed_daily(x, t0, ema50[x]))
                              for x in data])
        btc_px = close_at(btc, t0)
        rows.append({
            "symbol": base_of(s), "side": t.side, "entry_time": t0, "exit_time": pd.Timestamp(t.closed_at),
            "net_r": t.pnl / t.risk_usd if t.risk_usd else np.nan, "pnl": t.pnl,
            "mfe_r": t.r_at(t.mfe_price) if t.mfe_price else np.nan,
            "mae_r": t.r_at(t.mae_price) if t.mae_price else np.nan,
            "hold_h": (pd.Timestamp(t.closed_at) - pd.Timestamp(t.filled_at)).total_seconds() / 3600,
            "exit": t.exit_reason,
            "ext_ema50_pct": d * (px / e50 - 1) * 100,                       # how stretched above the daily EMA50
            "ret_7d_pct": d * (close_at(s, t0) / close_at(s, t0, pd.Timedelta(days=7)) - 1) * 100,
            "ret_30d_pct": d * (close_at(s, t0) / close_at(s, t0, pd.Timedelta(days=30)) - 1) * 100,
            "breakout_atr": t.setup.get("features", {}).get("breakout_atr"),
            "stop_pct": t.setup.get("features", {}).get("stop_pct"),
            "open_same_dir": open_same,                                       # crowding of the portfolio
            "signals_same_bar": int(signal_bars.get(t0, 1)),                  # market-wide breakout cluster
            "breadth": d * breadth,                                           # share of coins above EMA50 (signed)
            "btc_ret_7d_pct": d * (btc_px / close_at(btc, t0, pd.Timedelta(days=7)) - 1) * 100,
            "btc_vs_ema200": d * np.sign(last_closed_daily(btc, t0, d1[btc]["close"]) - last_closed_daily(btc, t0, ema200[btc])),
            "daily_close_vs_ema50_pct": d * (dclose / e50 - 1) * 100,
        })
    return pd.DataFrame(rows)


def bucket_report(df: pd.DataFrame) -> None:
    df = df.copy()
    df["year"] = np.where(df.entry_time < SPLIT, "24-25", "25-26")
    feats = ["ext_ema50_pct", "ret_7d_pct", "ret_30d_pct", "breakout_atr", "stop_pct", "open_same_dir",
             "signals_same_bar", "breadth", "btc_ret_7d_pct"]
    for f in feats:
        q = df[f].quantile([0, 1 / 3, 2 / 3, 1]).to_numpy()
        if len(np.unique(q)) < 4:
            df["b"] = df[f].clip(upper=df[f].quantile(0.9)).round(0)
        else:
            df["b"] = pd.cut(df[f], np.unique(q), include_lowest=True)
        t = df.groupby(["b", "year"], observed=True).net_r.agg(["size", "mean"]).round(3).unstack("year")
        print(f"\n== {f} (net R / işlem, işlem sayısı) ==")
        print(t.to_string())
    print("\n== btc_vs_ema200 (+1 = BTC trend yönümüzde) ==")
    print(df.groupby(["btc_vs_ema200", "year"]).net_r.agg(["size", "mean"]).round(3).unstack("year").to_string())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-week", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    syms = [ccxt_symbol(b, cfg.quote) for b in cfg.universe.symbols]
    if a.live_week:
        end = pd.Timestamp.now(tz="UTC").floor("4h")
        start = pd.Timestamp("2026-09-30 16:00", tz="UTC")
        md = MarketData(ccxt.binanceusdm({"enableRateLimit": True}))
        data = load_data(md, syms, start.floor("1D"), end, warmup_days=120)
        res = run_trend_backtest(cfg, data, start, end)
        df = context(data, res["trades"], cfg)
        pd.set_option("display.width", 250)
        cols = ["symbol", "side", "entry_time", "net_r", "mfe_r", "mae_r", "hold_h", "ext_ema50_pct", "ret_7d_pct",
                "ret_30d_pct", "breakout_atr", "open_same_dir", "signals_same_bar", "breadth", "btc_ret_7d_pct"]
        print(df[cols].round(2).to_string(index=False))
        btc = data[ccxt_symbol("BTC", cfg.quote)]["htf"]["close"]
        print("\nBTC 4H kapanışlar (son 10 gün):")
        print(btc[btc.index >= start - pd.Timedelta(days=4)].resample("12h").last().round(0).to_string())
        return
    start, end = pd.Timestamp("2024-09-30", tz="UTC"), pd.Timestamp("2026-09-30", tz="UTC")
    data = load_data_archive(syms, start, end, warmup_days=230)
    res = run_trend_backtest(cfg, data, start, end)
    df = context(data, res["trades"], cfg)
    df.to_pickle("data/research/trend_trades_context.pkl")
    print(f"{len(df)} işlem, ortalama net R {df.net_r.mean():+.3f}")
    bucket_report(df)


if __name__ == "__main__":
    main()
