from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Hard limits from the requirements (REQUIREMENTS.md): never risk more than 4% per trade,
# never target more than 3R.
HARD_MAX_RISK_PCT = 4.0
HARD_MAX_RR = 3.0


class Cfg(dict):
    """Dict with recursive attribute access (cfg.risk.max_rr)."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e

    def __setattr__(self, key, value):
        self[key] = value


def _wrap(obj):
    if isinstance(obj, dict):
        return Cfg({k: _wrap(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap(v) for v in obj]
    return obj


def load_config(path: str | Path = "config.yaml") -> Cfg:
    load_dotenv()
    with open(path, encoding="utf-8") as f:
        cfg = _wrap(yaml.safe_load(f))

    mode = os.getenv("BOT_MODE")
    if mode:
        cfg.mode = mode.strip().lower()
    if cfg.mode not in ("paper", "demo", "live"):
        raise ValueError(f"Geçersiz mode: {cfg.mode} (paper | demo | live)")

    cfg.secrets = Cfg(
        binance_key=os.getenv("BINANCE_API_KEY", ""),
        binance_secret=os.getenv("BINANCE_API_SECRET", ""),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        redis_url=os.getenv("REDIS_URL", ""),
        database_url=os.getenv("DATABASE_URL", ""),
    )
    _enforce_limits(cfg)
    return cfg


def _enforce_limits(cfg: Cfg) -> None:
    r = cfg.risk
    if r.max_risk_pct > HARD_MAX_RISK_PCT:
        log.warning("max_risk_pct %.2f > %.1f, sınırlandırıldı", r.max_risk_pct, HARD_MAX_RISK_PCT)
        r.max_risk_pct = HARD_MAX_RISK_PCT
    for tier in r.tiers:
        tier.risk_pct = min(float(tier.risk_pct), r.max_risk_pct)
    r.tiers = sorted(r.tiers, key=lambda t: -t.min_score)
    if r.max_rr > HARD_MAX_RR:
        log.warning("max_rr %.2f > %.1f, sınırlandırıldı", r.max_rr, HARD_MAX_RR)
        r.max_rr = HARD_MAX_RR
    r.base_rr = min(r.base_rr, r.max_rr)
    if r.max_open_positions < 1:
        r.max_open_positions = 1
