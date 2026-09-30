"""Event-driven backtester.

Uses exactly the same setup detection, risk sizing and position optimizer as the live bot,
stepping through 5-minute candles:  intrabar fills / SL / TP  ->  scan (new setups)  ->  optimize.

Conservative assumptions: stop before target when both are hit in the same 5m candle, stop
fills gap to the candle open plus slippage, a fill and a stop in the same candle counts as a loss.
Not simulated: the news filter (no historical calendar) and funding payments.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from .market_data import MarketData, resample
from .optimizer import build_context, optimize
from .risk import RiskGuard, choose_leverage, position_size, risk_pct_for_score
from .storage.kv import MemoryKV
from .strategy import find_setups, prepare
from .trade import Trade
from .utils import base_of, ccxt_symbol, index_ns, to_iso, ts_ns

log = logging.getLogger(__name__)

SLIPPAGE = 0.0002  # on stop / market exits
STEP_NS = 300 * 1_000_000_000


def _frames(df5: pd.DataFrame) -> dict:
    df5 = df5[["open", "high", "low", "close", "volume"]]
    return {"exec": df5, "ltf": resample(df5, "15m"), "mtf": resample(df5, "1h"), "htf": resample(df5, "4h"),
            "d1": resample(df5, "1d")}


def load_data(md: MarketData, symbols: list[str], start: pd.Timestamp, end: pd.Timestamp,
              warmup_days: int) -> dict[str, dict]:
    """History through the exchange API (ccxt)."""
    data = {}
    for sym in symbols:
        df5 = md.history(sym, "5m", start - pd.Timedelta(days=warmup_days), end)
        if len(df5) < 5000:
            log.warning("%s için yeterli veri yok, atlandı", sym)
            continue
        data[sym] = _frames(df5)
    return data


def load_data_archive(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp,
                      warmup_days: int) -> dict[str, dict]:
    """History from data.binance.vision (faster, includes funding; no API access needed)."""
    from .archive import funding, klines

    data = {}
    s = (start - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    e = end.strftime("%Y-%m-%d")
    for sym in symbols:
        raw = sym.split("/")[0] + "USDT"
        df5 = klines(raw, "5m", s, e)
        if len(df5) < 5000:
            log.warning("%s için arşivde yeterli veri yok, atlandı", sym)
            continue
        fr = _frames(df5)
        f = funding(raw, s, e)
        fr["funding"] = f["funding"] if not f.empty else None
        data[sym] = fr
        log.info("%s arşivden yüklendi (%d mum)", sym, len(df5))
    return data


def run_backtest(cfg, data: dict[str, dict], start: pd.Timestamp, end: pd.Timestamp,
                 use_optimizer: bool = True, start_balance: float = 1000.0,
                 trade_symbols: list[str] | None = None) -> dict:
    rc, oc = cfg.risk, cfg.optimizer
    preps = {sym: prepare(fr, cfg) for sym, fr in data.items()}
    btc, eth = ccxt_symbol("BTC", cfg.quote), ccxt_symbol("ETH", cfg.quote)
    setups_at: dict[int, list] = defaultdict(list)
    n_setups = 0
    for sym, P in preps.items():
        if trade_symbols and sym not in trade_symbols:
            continue
        ref = preps.get(eth if sym == btc else btc)
        for s in find_setups(sym, P, cfg, ref, ts_ns(start)):
            if s.created_at < end:
                setups_at[ts_ns(s.created_at)].append(s)
                n_setups += 1

    bars = {}
    for sym, fr in data.items():
        df = fr["exec"]
        bars[sym] = (dict(zip(index_ns(df.index).tolist(), range(len(df)))),
                     df["open"].to_numpy(float), df["high"].to_numpy(float),
                     df["low"].to_numpy(float), df["close"].to_numpy(float))
    timeline = sorted({t for sym in bars for t in bars[sym][0] if ts_ns(start) <= t < ts_ns(end)})

    balance = start_balance
    market_entry = cfg.strategy.entry_mode == "market"
    guard = RiskGuard(rc, MemoryKV())
    pending: dict[str, Trade] = {}
    open_: dict[str, Trade] = {}
    closed: list[Trade] = []
    cooldown: dict[str, int] = {}
    curve = []
    last_close: dict[str, float] = {}

    def bar(sym, t_open):
        m, o, h, lo, c = bars[sym]
        i = m.get(t_open)
        return None if i is None else (o[i], h[i], lo[i], c[i])

    def equity_now():
        eq = balance
        for t in open_.values():
            px = last_close.get(t.symbol, t.open_price)
            eq += t.dir * (px - t.open_price) * t.filled_qty
        return eq

    def close_trade(t: Trade, px: float, now: pd.Timestamp, reason: str, market: bool):
        nonlocal balance
        if market:
            px = px * (1 - t.dir * SLIPPAGE)
        gross = t.dir * (px - t.open_price) * t.filled_qty
        exit_fee = px * t.filled_qty * rc.fees.taker
        balance += gross - exit_fee
        t.fees += exit_fee
        t.status, t.closed_at, t.exit_price, t.exit_reason = "closed", to_iso(now), px, reason
        if t.mae_price is None or t.dir * (px - t.mae_price) < 0:
            t.mae_price = px
        t.pnl = gross - t.fees
        t.r_multiple = t.r_at(px)
        guard.register_close(t.pnl, now)
        cooldown[t.symbol] = ts_ns(now) + int(rc.symbol_cooldown_minutes * 60 * 1e9)
        open_.pop(t.symbol, None)
        closed.append(t)

    for t_open in timeline:
        t_close = t_open + STEP_NS
        now = pd.Timestamp(t_close, tz="UTC")

        # ---- 1) intrabar: pending fills / cancels ----
        for sym, t in list(pending.items()):
            b = bar(sym, t_open)
            if b is None:
                continue
            o, h, lo, c = b
            d = t.dir
            if (lo <= t.entry) if d == 1 else (h >= t.entry):
                fill = min(o, t.entry) if d == 1 else max(o, t.entry)
                t.status, t.fill_price, t.filled_qty = "open", fill, t.qty
                t.filled_at = to_iso(now)
                t.mfe_price = t.mae_price = fill
                fee = fill * t.qty * rc.fees.maker
                balance -= fee
                t.fees = fee
                pending.pop(sym)
                open_[sym] = t
                if (lo <= t.sl) if d == 1 else (h >= t.sl):
                    close_trade(t, t.sl, now, "SL (aynı mum)", True)
            elif ((h >= t.cancel_price) if d == 1 else (lo <= t.cancel_price)) or t_close >= ts_ns(pd.Timestamp(t.expires_at)):
                t.status = "cancelled"
                pending.pop(sym)

        # ---- 1b) intrabar: stops / targets ----
        for sym, t in list(open_.items()):
            if t.filled_at == to_iso(now):
                continue
            b = bar(sym, t_open)
            if b is None:
                continue
            o, h, lo, c = b
            d = t.dir
            if (lo <= t.sl) if d == 1 else (h >= t.sl):
                px = min(o, t.sl) if d == 1 else max(o, t.sl)
                r = t.r_at(t.sl)
                reason = "SL" if r < -0.2 else ("Break-even stop" if r <= 0.2 else "Kâr kilidi / trailing stop")
                close_trade(t, px, now, reason, True)
            elif (h >= t.tp) if d == 1 else (lo <= t.tp):
                close_trade(t, t.tp, now, "TP" + (" (uzatılmış)" if t.tp_extended else ""), False)
            else:
                fav, adv = (h, lo) if d == 1 else (lo, h)
                t.mfe_price = fav if d * (fav - t.mfe_price) > 0 else t.mfe_price
                t.mae_price = adv if d * (adv - t.mae_price) < 0 else t.mae_price

        for sym in bars:
            b = bar(sym, t_open)
            if b is not None:
                last_close[sym] = b[3]
        eq = equity_now()
        guard.on_cycle(eq, now)

        # ---- 2) scan: new setups closing at this time ----
        for s in sorted(setups_at.get(t_close, []), key=lambda x: -x.score):
            ok, _ = guard.can_open(now)
            if not ok or s.symbol in open_ or s.symbol in pending:
                continue
            if cooldown.get(s.symbol, 0) > t_close:
                continue
            slots = rc.max_open_positions - len(open_) - len(pending)
            if slots <= 0:
                continue
            px = last_close.get(s.symbol)
            if px is None or (not market_entry and s.dir * (px - s.entry) <= 0):
                continue
            risk_pct = risk_pct_for_score(s.score, rc)
            open_risk = sum(t.current_risk_usd() for t in open_.values()) + sum(t.risk_usd for t in pending.values())
            room = eq * rc.max_total_risk_pct / 100 - open_risk
            if room < eq * risk_pct / 100:
                if room < eq * 0.005:
                    continue
                risk_pct = room / eq * 100
            qty, _ = position_size(eq, s.entry, s.sl, risk_pct, rc.fees)
            used_margin = sum(t.filled_qty * t.open_price / t.leverage for t in open_.values())
            used_margin += sum(t.qty * t.entry / t.leverage for t in pending.values())
            plan = choose_leverage(qty * s.entry, max(eq - used_margin, 0), slots, abs(s.entry - s.sl) / s.entry,
                                   rc.leverage_max)
            qty *= plan.qty_scale
            if qty * s.entry < 5:
                continue
            risk_usd = qty * (abs(s.entry - s.sl) + s.entry * (rc.fees.maker + rc.fees.taker))
            t = Trade(
                id=f"{base_of(s.symbol)}-{s.created_at:%Y%m%d%H%M}-{s.side[0].upper()}", symbol=s.symbol,
                side=s.side, status="pending", setup_id=s.id, score=s.score, reasons=s.reasons, poi=s.poi,
                swept=s.swept, entry=s.entry, sl=s.sl, tp=s.tp, initial_sl=s.sl, initial_tp=s.tp, tp_r=s.tp_r,
                qty=qty, risk_pct=risk_usd / eq * 100, risk_usd=risk_usd, leverage=plan.leverage,
                created_at=to_iso(s.created_at), expires_at=to_iso(s.expires_at), cancel_price=s.tp,
                targets=s.targets, setup=s.to_dict(),
            )
            if market_entry:  # filled on the MSS close (+ slippage, taker fee)
                fill = s.entry * (1 + s.dir * SLIPPAGE)
                t.status, t.fill_price, t.filled_qty, t.filled_at = "open", fill, qty, to_iso(now)
                t.mfe_price = t.mae_price = fill
                t.fees = fill * qty * rc.fees.taker
                balance -= t.fees
                open_[s.symbol] = t
            else:
                pending[s.symbol] = t

        # ---- 3) optimize open positions ----
        if use_optimizer and oc.enabled:
            for sym, t in list(open_.items()):
                px = last_close.get(sym)
                if px is None:
                    continue
                ctx = build_context(preps[sym], t, now, px, None)
                for a in optimize(t, ctx, oc, rc.max_rr):
                    if a.kind == "close":
                        close_trade(t, px, now, "Optimizasyon: " + a.reason, True)
                        break
                    if a.kind == "move_sl":
                        t.sl = a.price
                        if t.r_at(a.price) >= 0:
                            t.be_done = True
                    elif a.kind == "set_tp":
                        t.tp = a.price
                        t.tp_extended = True

        if t_open % (3600 * 1_000_000_000) == 0:
            curve.append((now, equity_now()))

    end_ts = pd.Timestamp(timeline[-1] + STEP_NS, tz="UTC") if timeline else end
    for sym, t in list(open_.items()):
        close_trade(t, last_close.get(sym, t.open_price), end_ts, "Backtest sonu", True)
    curve.append((end_ts, balance))
    return {"trades": closed, "curve": curve, "start_balance": start_balance, "end_balance": balance,
            "setups": n_setups, "start": start, "end": end, "optimizer": use_optimizer}


def run_trend_backtest(cfg, data: dict[str, dict], start: pd.Timestamp, end: pd.Timestamp,
                       start_balance: float = 1000.0, trade_symbols: list[str] | None = None) -> dict:
    """4H trend strategy: stop orders checked on each 4H candle's range, trailing stop updated on
    the close, entries at the breakout candle close. Funding is charged when archive data has it."""
    from . import trend

    tc, fees = cfg.strategies.trend, cfg.risk.fees
    h4_ns = 4 * 3600 * 1_000_000_000
    states, bars, sig_at, n_signals = {}, {}, defaultdict(list), 0
    for sym, fr in data.items():
        st = trend.analyze(fr["htf"], fr.get("d1") if fr.get("d1") is not None else resample(fr["exec"], "1d"), tc)
        states[sym] = st
        bars[sym] = dict(zip(index_ns(fr["htf"].index).tolist(), range(len(fr["htf"]))))
        if trade_symbols and sym not in trade_symbols:
            continue
        for s in trend.signals(sym, st, tc, ts_ns(start)):
            if s.created_at < end:
                sig_at[ts_ns(s.created_at)].append(s)
                n_signals += 1
    order = {sym: i for i, sym in enumerate(data)}
    timeline = sorted({t for sym in bars for t in bars[sym] if ts_ns(start) <= t < ts_ns(end)})

    balance = start_balance
    guard = RiskGuard(tc.guard, MemoryKV())
    open_: dict[str, Trade] = {}
    best: dict[str, float] = {}
    closed: list[Trade] = []
    curve = []
    last_close: dict[str, float] = {}

    def ohlc(sym, t_open):
        i = bars[sym].get(t_open)
        if i is None:
            return None, None
        h4 = data[sym]["htf"]
        return i, (h4["open"].iat[i], h4["high"].iat[i], h4["low"].iat[i], h4["close"].iat[i])

    def equity_now():
        return balance + sum(t.dir * (last_close.get(t.symbol, t.open_price) - t.open_price) * t.filled_qty
                             for t in open_.values())

    def close_trade(t: Trade, px: float, now: pd.Timestamp, reason: str):
        nonlocal balance
        px *= 1 - t.dir * SLIPPAGE
        gross = t.dir * (px - t.open_price) * t.filled_qty
        fee = px * t.filled_qty * fees.taker
        balance += gross - fee
        t.fees += fee
        t.status, t.closed_at, t.exit_price, t.exit_reason = "closed", to_iso(now), px, reason
        if t.mae_price is None or t.dir * (px - t.mae_price) < 0:
            t.mae_price = px
        t.pnl = gross - t.fees
        t.r_multiple = t.r_at(px)
        guard.register_close(t.pnl, now)
        open_.pop(t.symbol, None)
        closed.append(t)

    for t_open in timeline:
        t_close = t_open + h4_ns
        now = pd.Timestamp(t_close, tz="UTC")
        # 1) funding + stop orders during the candle
        for sym, t in list(open_.items()):
            i, b = ohlc(sym, t_open)
            if b is None:
                continue
            o, h, lo, c = b
            f = data[sym].get("funding")
            if f is not None:
                rates = f[(f.index > pd.Timestamp(t_open, tz="UTC")) & (f.index <= now)]
                if len(rates):
                    cost = float(rates.sum()) * t.dir * t.filled_qty * c
                    balance -= cost
                    t.fees += cost
            if (lo <= t.sl) if t.dir == 1 else (h >= t.sl):
                px = min(o, t.sl) if t.dir == 1 else max(o, t.sl)
                close_trade(t, px, now, "SL" if t.r_at(t.sl) < -0.2 else "Trailing stop")
        for sym in bars:
            _, b = ohlc(sym, t_open)
            if b is not None:
                last_close[sym] = b[3]
        eq = equity_now()
        guard.on_cycle(eq, now)
        # 2) trailing stop update for positions opened before this candle
        for sym, t in list(open_.items()):
            i, b = ohlc(sym, t_open)
            if b is None:
                continue
            o, h, lo, c = b
            fav, adv = (h, lo) if t.dir == 1 else (lo, h)
            t.mfe_price = fav if t.dir * (fav - t.mfe_price) > 0 else t.mfe_price
            t.mae_price = adv if t.dir * (adv - t.mae_price) < 0 else t.mae_price
            best[sym] = max(best[sym], c) if t.dir == 1 else min(best[sym], c)
            cand = trend.chandelier(best[sym], states[sym].atr[i], t.dir, tc.atr_mult)
            if t.dir * (c - cand) <= 0:
                close_trade(t, c, now, "Trailing stop")
            elif t.dir * (cand - t.sl) >= tc.min_stop_step_r * t.risk_unit:
                t.sl = cand
                t.be_done = t.be_done or t.r_at(cand) >= 0
        # 3) new breakouts at this close
        for s in sorted(sig_at.get(t_close, []), key=lambda x: order[x.symbol]):
            if not guard.can_open(now)[0] or s.symbol in open_ or len(open_) >= tc.max_open_positions:
                continue
            risk_pct = tc.risk_pct_long if s.side == "long" else tc.risk_pct_short
            room = eq * tc.max_total_risk_pct / 100 - sum(t.current_risk_usd() for t in open_.values())
            if room < eq * risk_pct / 100:
                if room < eq * 0.002:
                    continue
                risk_pct = room / eq * 100
            qty, _ = position_size(eq, s.entry, s.sl, risk_pct, fees)
            if qty * s.entry < 5:
                continue
            fill = s.entry * (1 + s.dir * SLIPPAGE)
            fee = fill * qty * fees.taker
            balance -= fee
            risk_usd = qty * (abs(s.entry - s.sl) + s.entry * (fees.maker + fees.taker))
            t = Trade(id=f"{base_of(s.symbol)}-{s.created_at:%Y%m%d%H%M}-T{s.side[0].upper()}", symbol=s.symbol,
                      side=s.side, status="open", setup_id=s.id, score=0, reasons=s.reasons, poi=s.poi, swept=[],
                      entry=s.entry, sl=s.sl, tp=None, initial_sl=s.sl, initial_tp=None, tp_r=0.0, qty=qty,
                      risk_pct=risk_usd / eq * 100, risk_usd=risk_usd, leverage=1, created_at=to_iso(s.created_at),
                      expires_at=to_iso(s.expires_at), cancel_price=None, setup=s.to_dict(), strategy="trend",
                      fill_price=fill, filled_qty=qty, filled_at=to_iso(now), fees=fee)
            t.mfe_price = t.mae_price = fill
            open_[s.symbol] = t
            best[s.symbol] = s.entry
        if t_open % (86400 * 1_000_000_000) == 0:
            curve.append((now, equity_now()))

    end_ts = pd.Timestamp(timeline[-1] + h4_ns, tz="UTC") if timeline else end
    for sym, t in list(open_.items()):
        close_trade(t, last_close.get(sym, t.open_price), end_ts, "Backtest sonu")
    curve.append((end_ts, balance))
    return {"trades": closed, "curve": curve, "start_balance": start_balance, "end_balance": balance,
            "setups": n_signals, "start": start, "end": end, "optimizer": True, "strategy": "trend"}


# ---------------------------------------------------------------------- reporting
def trades_frame(trades: list[Trade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        hold = (pd.Timestamp(t.closed_at) - pd.Timestamp(t.filled_at)).total_seconds() / 60
        rows.append({
            "id": t.id, "symbol": base_of(t.symbol), "side": t.side, "score": t.score, "poi": t.poi,
            "swept": "|".join(t.swept), "created_at": t.created_at, "filled_at": t.filled_at,
            "closed_at": t.closed_at, "entry": t.open_price, "initial_sl": t.initial_sl, "exit": t.exit_price,
            "exit_reason": t.exit_reason, "r": round(t.r_multiple, 3),
            "net_r": round(t.pnl / t.risk_usd, 3) if t.risk_usd else None, "pnl": round(t.pnl, 2),
            "risk_pct": round(t.risk_pct, 2), "mfe_r": round(t.r_at(t.mfe_price), 2) if t.mfe_price else None,
            "mae_r": round(t.r_at(t.mae_price), 2) if t.mae_price else None, "hold_min": round(hold, 1),
            "be_done": t.be_done, "tp_extended": t.tp_extended,
            "killzone": t.setup.get("features", {}).get("killzone"), "smt": t.setup.get("features", {}).get("smt"),
        })
    return pd.DataFrame(rows)


def summarize(res: dict) -> dict:
    df = trades_frame(res["trades"])
    curve = pd.Series([v for _, v in res["curve"]], dtype=float)
    dd = float(((curve.cummax() - curve) / curve.cummax()).max() * 100) if len(curve) else 0.0
    out = {
        "period": f"{res['start']:%Y-%m-%d} → {res['end']:%Y-%m-%d}",
        "optimizer": res["optimizer"],
        "setups_detected": res["setups"],
        "trades": int(len(df)),
        "start_balance": round(res["start_balance"], 2),
        "end_balance": round(res["end_balance"], 2),
        "return_pct": round((res["end_balance"] / res["start_balance"] - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
    }
    if len(df):
        wins = df[df.pnl > 0]
        losses = df[df.pnl <= 0]
        out.update({
            "win_rate_pct": round(len(wins) / len(df) * 100, 1),
            "avg_r": round(df.r.mean(), 3),
            "avg_net_r": round(df.net_r.mean(), 3),
            "total_r": round(df.r.sum(), 2),
            "profit_factor": round(wins.pnl.sum() / -losses.pnl.sum(), 2) if losses.pnl.sum() < 0 else None,
            "avg_hold_hours": round(df.hold_min.mean() / 60, 2),
            "min_hold_minutes": round(df.hold_min.min(), 1),
            "avg_mfe_r": round(df.mfe_r.mean(), 2),
            "trades_per_week": round(len(df) / max((res["end"] - res["start"]).days / 7, 1e-9), 1),
            "exit_reasons": dict(Counter(r.split(" (")[0] for r in df.exit_reason)),
            "by_symbol": df.groupby("symbol").agg(trades=("r", "size"), avg_r=("r", "mean"), pnl=("pnl", "sum"))
                           .round(2).to_dict("index"),
            "by_side": df.groupby("side").agg(trades=("r", "size"), avg_r=("r", "mean")).round(3).to_dict("index"),
        })
    return out


def save_results(res: dict, summary: dict, out_dir: str = "backtests") -> Path:
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    tag = (pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"
           + ("" if res["optimizer"] else "_noopt"))
    trades_frame(res["trades"]).to_csv(p / f"bt_{tag}_trades.csv", index=False)
    (p / f"bt_{tag}_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(res["curve"], columns=["time", "equity"]).to_csv(p / f"bt_{tag}_equity.csv", index=False)
    return p / f"bt_{tag}_summary.json"


def to_journal(journal, res: dict) -> None:
    """Store backtest trades in PostgreSQL (mode = backtest-...) so the same analysis views work."""
    for t in res["trades"]:
        journal.upsert_trade(t)
