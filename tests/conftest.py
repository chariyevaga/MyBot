import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smcbot.config import load_config  # noqa: E402


@pytest.fixture
def cfg(monkeypatch):
    for k in ("BOT_MODE", "REDIS_URL", "DATABASE_URL", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def random_walk_5m():
    """20 days of synthetic 5m candles (geometric random walk with volatility clusters)."""
    rng = np.random.default_rng(7)
    n = 20 * 288
    vol = 0.0015 * (1 + 0.8 * np.sin(np.arange(n) / 500) ** 2)
    rets = rng.normal(0, vol)
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[100], close[:-1]])
    spread = np.abs(rng.normal(0, vol * 0.7)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    idx = pd.date_range("2026-01-05", periods=n, freq="5min", tz="UTC").as_unit("ns")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(50, 150, n)}, index=idx)
