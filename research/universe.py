"""Pick the trading universe: long-listed crypto perpetuals with the deepest volume.

    python -m research.universe --top 30
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from research.archive import klines

EXCLUDE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "BTCDOM", "DEFI", "FOOTBALL", "BLUEBIRD"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--listed-before", default="2024-07-01")
    p.add_argument("--volume-months", default="2026-06-01:2026-09-01")
    p.add_argument("--symbols-file", default=None)
    a = p.parse_args()

    if a.symbols_file:
        symbols = Path(a.symbols_file).read_text().split()
    else:
        from research.archive_list import list_symbols
        symbols = list_symbols()
    symbols = [s for s in symbols if s.endswith("USDT") and s[:-4] not in EXCLUDE]
    vol_start, vol_end = a.volume_months.split(":")
    first = pd.Timestamp(a.listed_before)

    def score(sym: str):
        old = klines(sym, "1d", str((first - pd.Timedelta(days=31)).date()), str(first.date()), workers=1)
        if old.empty:
            return None
        recent = klines(sym, "1d", vol_start, vol_end, workers=1)
        if len(recent) < 60:
            return None
        return sym, float(recent["quote_volume"].mean()), float((recent["high"] / recent["low"] - 1).mean() * 100)

    with ThreadPoolExecutor(16) as ex:
        rows = [r for r in ex.map(score, symbols) if r]
    df = pd.DataFrame(rows, columns=["symbol", "avg_daily_quote_volume", "avg_daily_range_pct"])
    df = df.sort_values("avg_daily_quote_volume", ascending=False).head(a.top).reset_index(drop=True)
    df["avg_daily_quote_volume"] = (df["avg_daily_quote_volume"] / 1e6).round(0)
    print(df.to_string())
    print("\nBASES:", ",".join(s[:-4] for s in df.symbol))


if __name__ == "__main__":
    main()
