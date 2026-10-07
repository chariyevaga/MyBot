"""Compare trend-strategy variants on monthly consistency (target: >= +2% per month).

Loads 2 years of archive data once and runs every variant for each year separately.

    python -m research.optimize
"""
from __future__ import annotations

import argparse
import copy
import time

import pandas as pd

from smcbot.archive import funding, klines
from smcbot.config import _wrap, load_config
from smcbot.backtest import run_trend_backtest
from smcbot.market_data import resample
from smcbot.utils import ccxt_symbol

YEARS = {"24-25": ("2024-09-30", "2025-09-30"), "25-26": ("2025-09-30", "2026-09-30")}
# untouched out-of-sample year (rules were found on 24-25 and 25-26): python -m research.optimize --oos
OOS_YEARS = {"23-24": ("2023-09-30", "2024-09-30")}

VARIANTS: dict[str, dict] = {
    "A mevcut": {},
    "R1 zayıf kırılım (<0.2 ATR) yok": {"rules": [{"feature": "breakout_atr", "below": 0.2, "risk_mult": 0}]},
    "R2 zayıf kırılım yarım risk": {"rules": [{"feature": "breakout_atr", "below": 0.2, "risk_mult": 0.5}]},
    "R3 7g>%9 yarım risk": {"rules": [{"feature": "ret_7d_pct", "above": 9, "risk_mult": 0.5}]},
    "R4 stop>%9 yok": {"rules": [{"feature": "stop_pct", "above": 9, "risk_mult": 0}]},
    "R5 R2+R3+R4": {"rules": [{"feature": "breakout_atr", "below": 0.2, "risk_mult": 0.5},
                              {"feature": "ret_7d_pct", "above": 9, "risk_mult": 0.5},
                              {"feature": "stop_pct", "above": 9, "risk_mult": 0}]},
    "B1 BTC rejim EMA200": {"btc_regime_ema": 200},
    "B2 BTC rejim EMA50": {"btc_regime_ema": 50},
    "D1 düşüş %10 → risk ×0.5": {"dd_risk_cut": [[10, 0.5]]},
    "P1 +2R'de yarı kâr al": {"partial_tp_r": 2.0, "partial_tp_frac": 0.5},
    "P2 +3R'de yarı kâr al": {"partial_tp_r": 3.0, "partial_tp_frac": 0.5},
    "L sadece long": {"allow_short": False},
    "L+R5 sadece long + kurallar": {"allow_short": False,
                                     "rules": [{"feature": "breakout_atr", "below": 0.2, "risk_mult": 0.5},
                                               {"feature": "ret_7d_pct", "above": 9, "risk_mult": 0.5},
                                               {"feature": "stop_pct", "above": 9, "risk_mult": 0}]},
}


def load(symbols: list[str], start: str, end: str) -> dict:
    data = {}
    for sym in symbols:
        raw = sym.split("/")[0] + "USDT"
        k = klines(raw, "5m", start, end)[["open", "high", "low", "close", "volume"]]
        f = funding(raw, start, end)
        data[sym] = {"htf": resample(k, "4h"), "d1": resample(k, "1d"),
                     "funding": f["funding"] if not f.empty else None}
    return data


def monthly(res: dict) -> pd.Series:
    eq = pd.Series([v for _, v in res["curve"]], index=[t for t, _ in res["curve"]])
    return eq.resample("ME").last().pct_change().dropna() * 100


def metrics(res: dict) -> dict:
    m = monthly(res)
    eq = pd.Series([v for _, v in res["curve"]])
    dd = float(((eq.cummax() - eq) / eq.cummax()).max() * 100)
    return {"yıl%": round((res["end_balance"] / res["start_balance"] - 1) * 100, 1), "dd%": round(dd, 1),
            "ay_ort%": round(m.mean(), 2), "ay>=2%": int((m >= 2).sum()), "neg_ay": int((m < 0).sum()),
            "en_kötü_ay%": round(m.min(), 1), "işlem": len(res["trades"])}


def run_variant(cfg, data, overrides: dict, years: dict = YEARS) -> dict:
    c = copy.deepcopy(cfg)
    for k, v in overrides.items():
        c.strategies.trend[k] = _wrap(v)
    out = {}
    for y, (s, e) in years.items():
        res = run_trend_backtest(c, data, pd.Timestamp(s, tz="UTC"), pd.Timestamp(e, tz="UTC"))
        out[y] = metrics(res)
        out[y]["_monthly"] = monthly(res)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="comma-separated variant name prefixes")
    ap.add_argument("--oos", action="store_true", help="run on the untouched 2023-24 year")
    a = ap.parse_args()
    years = OOS_YEARS if a.oos else YEARS
    cfg = load_config()
    syms = [ccxt_symbol(b, cfg.quote) for b in cfg.universe.symbols]
    t0 = time.time()
    data = load(syms, "2023-03-01" if a.oos else "2024-04-01", "2024-10-01" if a.oos else "2026-09-30")
    print(f"veri yüklendi ({time.time() - t0:.0f}s)", flush=True)
    rows = []
    for name, ov in VARIANTS.items():
        if a.only and not any(name.startswith(p) for p in a.only.split(",")):
            continue
        t1 = time.time()
        r = run_variant(cfg, data, ov, years)
        for y in years:
            rows.append({"varyant": name, "yıl": y, **{k: v for k, v in r[y].items() if not k.startswith("_")}})
        print(f"{name:34s} " + " | ".join(f"{y}: {r[y]['yıl%']:+6.1f}% ay>=2%: {r[y]['ay>=2%']:2d} neg: {r[y]['neg_ay']:2d}"
                                           for y in years) + f"  ({time.time() - t1:.0f}s)", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(f"data/research/optimize_results{'_oos' if a.oos else ''}.csv", index=False)
    pd.set_option("display.width", 200)
    print("\n" + df.pivot(index="varyant", columns="yıl").swaplevel(axis=1).sort_index(axis=1).to_string())


if __name__ == "__main__":
    main()
