"""Position sizing, leverage selection and account-level circuit breakers."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .utils import from_iso, to_iso


def risk_pct_for_score(score: int, risk_cfg) -> float:
    for tier in risk_cfg.tiers:  # sorted by min_score desc in config loader
        if score >= tier.min_score:
            return min(float(tier.risk_pct), float(risk_cfg.max_risk_pct))
    return 0.0


def position_size(equity: float, entry: float, sl: float, risk_pct: float, fees) -> tuple[float, float]:
    """Quantity such that hitting the stop (incl. estimated fees) loses ``risk_pct`` of equity."""
    risk_usd = equity * risk_pct / 100.0
    per_unit = abs(entry - sl) + entry * (fees.maker + fees.taker)
    if per_unit <= 0:
        return 0.0, 0.0
    return risk_usd / per_unit, risk_usd


@dataclass
class LeveragePlan:
    leverage: int
    qty_scale: float  # < 1 when the margin budget cannot hold the full size


def choose_leverage(notional: float, free_balance: float, slots_left: int, sl_dist_frac: float,
                    leverage_max: int, maint_margin: float = 0.005) -> LeveragePlan:
    """Smallest leverage that fits the margin budget while keeping liquidation well beyond the stop.

    Isolated margin: liquidation is roughly ``1/leverage - maintenance`` away from entry. We require
    the stop distance to be at most 70% of that.
    """
    budget = max(free_balance * 0.95 / max(slots_left, 1), 1e-9)
    need = max(1, math.ceil(notional / budget))
    liq_cap = math.floor(1.0 / (sl_dist_frac / 0.7 + maint_margin)) if sl_dist_frac > 0 else leverage_max
    lev_max = max(1, min(leverage_max, liq_cap))
    if need <= lev_max:
        return LeveragePlan(need, 1.0)
    return LeveragePlan(lev_max, (budget * lev_max) / notional)


class RiskGuard:
    """Daily loss limit, losing-streak cooldown, drawdown halt. State lives in the KV store."""

    def __init__(self, risk_cfg, kv, key: str = "risk"):
        self.cfg = risk_cfg
        self.kv = kv
        self.key = key
        self.s = kv.get_json(key) or {}

    def _save(self) -> None:
        self.kv.set_json(self.key, self.s)

    def on_cycle(self, equity: float, now: pd.Timestamp) -> None:
        day = now.strftime("%Y-%m-%d")
        if self.s.get("day") != day:
            self.s.update(day=day, day_start_equity=equity, realized_today=0.0, trades_today=0)
        self.s["peak_equity"] = max(float(self.s.get("peak_equity") or equity), equity)
        self.s["last_equity"] = equity
        peak = self.s["peak_equity"]
        if peak > 0 and (peak - equity) / peak * 100 >= self.cfg.max_drawdown_pct and not self.s.get("halted"):
            self.s["halted"] = True
            self.s["halt_reason"] = f"Maksimum drawdown %{self.cfg.max_drawdown_pct} aşıldı"
        self._save()

    def register_close(self, pnl: float, now: pd.Timestamp) -> str | None:
        """Returns a message if a breaker was triggered."""
        self.s["realized_today"] = float(self.s.get("realized_today", 0.0)) + pnl
        self.s["trades_today"] = int(self.s.get("trades_today", 0)) + 1
        msg = None
        if pnl < 0:
            self.s["consecutive_losses"] = int(self.s.get("consecutive_losses", 0)) + 1
            if self.cfg.max_consecutive_losses and self.s["consecutive_losses"] >= self.cfg.max_consecutive_losses:
                until = now + pd.Timedelta(hours=self.cfg.cooldown_hours)
                self.s["paused_until"] = to_iso(until)
                self.s["consecutive_losses"] = 0
                msg = (f"{self.cfg.max_consecutive_losses} ardışık zarar: yeni işlemler "
                       f"{self.cfg.cooldown_hours} saat durduruldu")
        else:
            self.s["consecutive_losses"] = 0
        start_eq = float(self.s.get("day_start_equity") or 0)
        if start_eq > 0 and -self.s["realized_today"] >= start_eq * self.cfg.max_daily_loss_pct / 100:
            msg = f"Günlük zarar limiti (%{self.cfg.max_daily_loss_pct}) doldu: bugün yeni işlem yok"
        self._save()
        return msg

    def can_open(self, now: pd.Timestamp) -> tuple[bool, str]:
        if self.s.get("halted"):
            return False, self.s.get("halt_reason", "Bot durduruldu")
        until = from_iso(self.s.get("paused_until"))
        if until is not None and now < until:
            return False, f"Ardışık zarar molası ({until:%H:%M} UTC'ye kadar)"
        start_eq = float(self.s.get("day_start_equity") or 0)
        if start_eq > 0 and -float(self.s.get("realized_today", 0)) >= start_eq * self.cfg.max_daily_loss_pct / 100:
            return False, "Günlük zarar limiti doldu"
        return True, ""

    def resume(self) -> None:
        self.s["halted"] = False
        self.s.pop("halt_reason", None)
        self.s["paused_until"] = None
        self.s["peak_equity"] = self.s.get("last_equity") or self.s.get("peak_equity")
        self._save()
