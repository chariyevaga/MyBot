"""Download 5m klines, OI/long-short metrics and funding for the research universe.

    python -m research.download --start 2024-07-01 --end 2026-09-30
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from research.archive import funding, klines, metrics

UNIVERSE = ("BTC,ETH,SOL,XRP,DOGE,BNB,ADA,LINK,AVAX,TRX,ZEC,WLD,1000PEPE,NEAR,ENA,SUI,TAO,XLM,AAVE,UNI,"
            "BCH,ONDO,FIL,LTC,DOT,1000SHIB,INJ,XMR,ARB,FET").split(",")
OUT = Path("data/research")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-07-01")
    p.add_argument("--end", default="2026-09-30")
    p.add_argument("--symbols", default=",".join(UNIVERSE))
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for base in a.symbols.split(","):
        sym = base + "USDT"
        t0 = time.time()
        k = klines(sym, "5m", a.start, a.end)
        m = metrics(sym, a.start, a.end)
        f = funding(sym, a.start, a.end)
        k.to_pickle(OUT / f"{base}_5m.pkl")
        m.to_pickle(OUT / f"{base}_metrics.pkl")
        f.to_pickle(OUT / f"{base}_funding.pkl")
        print(f"{base:9s} klines={len(k):7d} ({k.index.min():%Y-%m-%d}→{k.index.max():%Y-%m-%d}) "
              f"metrics={len(m):7d} funding={len(f):5d}  {time.time() - t0:5.0f}s", flush=True)


if __name__ == "__main__":
    main()
