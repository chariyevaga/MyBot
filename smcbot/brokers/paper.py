"""Paper broker: a simulated futures account driven by real Binance prices (1m candles).

Conservative fill rules: limit orders fill only when price trades through them; if a stop and
a take-profit are both touched in the same minute the stop wins; stops that gap fill at the
candle open.
"""
from __future__ import annotations

import logging
import time
import uuid

import ccxt

from ..market_data import MarketData
from .base import Broker, CloseResult, OrderState, PositionInfo, StopWouldTrigger

log = logging.getLogger(__name__)

KEY = "paper:account"


class PaperBroker(Broker):
    def __init__(self, md: MarketData, kv, start_balance: float, fees):
        self.md = md
        self.exchange = md.ex
        if not self.exchange.markets:
            self.exchange.load_markets()
        self.kv = kv
        self.fees = fees
        self.a = kv.get_json(KEY) or {
            "balance": float(start_balance), "orders": {}, "positions": {}, "closed": [], "cursor": {}, "last": {},
        }
        self._save()

    def _save(self):
        self.kv.set_json(KEY, self.a)

    # ------------------------------------------------------------------
    def sync(self) -> None:
        symbols = {o["symbol"] for o in self.a["orders"].values()} | set(self.a["positions"])
        for sym in symbols:
            try:
                self._sync_symbol(sym)
            except ccxt.BaseError as e:
                log.warning("Paper sync %s: %s", sym, e)
        self._save()

    def _sync_symbol(self, sym: str) -> None:
        now_ms = int(time.time() * 1000)
        cursor = int(self.a["cursor"].get(sym, now_ms - 60_000))
        df = self.md.candles_since(sym, "1m", cursor)
        for ts, row in df.iterrows():
            t_ms = int(ts.value // 1_000_000)
            if t_ms < cursor:
                continue
            self._process_candle(sym, float(row.open), float(row.high), float(row.low), float(row.close), t_ms)
            self.a["cursor"][sym] = t_ms + 60_000
            self.a["last"][sym] = float(row.close)

    def _process_candle(self, sym, o, h, lo, c, t_ms):
        # 1) entries
        for oid, od in list(self.a["orders"].items()):
            if od["symbol"] != sym or od["type"] != "entry" or od["created_ms"] > t_ms + 60_000:
                continue
            px = od["price"]
            if od["side"] == "long" and lo <= px:
                self._fill_entry(oid, od, min(px, o), t_ms)
            elif od["side"] == "short" and h >= px:
                self._fill_entry(oid, od, max(px, o), t_ms)
        # 2) protective orders
        pos = self.a["positions"].get(sym)
        if not pos:
            return
        sl = next((od for od in self.a["orders"].values() if od["symbol"] == sym and od["type"] == "sl"), None)
        tp = next((od for od in self.a["orders"].values() if od["symbol"] == sym and od["type"] == "tp"), None)
        if pos["side"] == "long":
            if sl and lo <= sl["price"]:
                self._close(sym, min(sl["price"], o), t_ms, "SL")
            elif tp and h >= tp["price"]:
                self._close(sym, max(tp["price"], o), t_ms, "TP")
        else:
            if sl and h >= sl["price"]:
                self._close(sym, max(sl["price"], o), t_ms, "SL")
            elif tp and lo <= tp["price"]:
                self._close(sym, min(tp["price"], o), t_ms, "TP")

    def _fill_entry(self, oid, od, px, t_ms, taker: bool = False):
        sym = od["symbol"]
        qty = od["qty"]
        fee = qty * px * (self.fees.taker if taker else self.fees.maker)
        pos = self.a["positions"].get(sym)
        if pos:  # add to position
            tot = pos["qty"] + qty
            pos["entry"] = (pos["entry"] * pos["qty"] + px * qty) / tot
            pos["qty"] = tot
            pos["fees"] += fee
        else:
            self.a["positions"][sym] = {"side": od["side"], "qty": qty, "entry": px, "fees": fee, "opened_ms": t_ms}
        self.a["balance"] -= fee
        od["filled"] = qty
        od["average"] = px
        od["status"] = "filled"
        self.a["orders"].pop(oid, None)
        self.a.setdefault("done_orders", {})[oid] = od
        self._trim_done()
        log.info("[PAPER] %s %s giriş doldu @ %s", sym, od["side"], px)

    def _trim_done(self):
        done = self.a.get("done_orders", {})
        if len(done) > 200:
            for k in list(done)[:-200]:
                done.pop(k, None)

    def _close(self, sym, px, t_ms, why):
        pos = self.a["positions"].pop(sym)
        d = 1 if pos["side"] == "long" else -1
        gross = d * (px - pos["entry"]) * pos["qty"]
        fee = pos["qty"] * px * self.fees.taker
        self.a["balance"] += gross - fee
        total_fees = pos["fees"] + fee
        self.a["closed"].append({"symbol": sym, "exit": px, "pnl": gross - total_fees, "fees": total_fees,
                                 "ts": t_ms, "why": why})
        self.a["closed"] = self.a["closed"][-200:]
        for oid, od in list(self.a["orders"].items()):
            if od["symbol"] == sym and od["type"] in ("sl", "tp"):
                self.a["orders"].pop(oid)
        log.info("[PAPER] %s kapandı (%s) @ %s  PnL %.2f", sym, why, px, gross - total_fees)

    # ------------------------------------------------------------------
    def _mark(self, sym: str) -> float:
        try:
            px = self.md.last_price(sym)
            self.a["last"][sym] = px
            return px
        except ccxt.BaseError:
            return float(self.a["last"].get(sym) or self.a["positions"].get(sym, {}).get("entry") or 0)

    def equity(self) -> float:
        eq = self.a["balance"]
        for sym, p in self.a["positions"].items():
            d = 1 if p["side"] == "long" else -1
            eq += d * (float(self.a["last"].get(sym, p["entry"])) - p["entry"]) * p["qty"]
        return eq

    def free_balance(self) -> float:
        return self.a["balance"]  # margin is not simulated

    def positions(self):
        out = {}
        for sym, p in self.a["positions"].items():
            d = 1 if p["side"] == "long" else -1
            last = float(self.a["last"].get(sym, p["entry"]))
            out[sym] = PositionInfo(sym, p["side"], p["qty"], p["entry"], d * (last - p["entry"]) * p["qty"], last)
        return out

    def prepare(self, symbol, leverage):
        pass

    def place_entry(self, symbol, side, qty, price, client_id, order_type="limit"):
        oid = "P-" + uuid.uuid4().hex[:12]
        now_ms = int(time.time() * 1000)
        self.a["orders"][oid] = {"symbol": symbol, "type": "entry", "side": side, "qty": qty,
                                 "price": self.round_price(symbol, price), "created_ms": now_ms, "status": "open"}
        self.a["cursor"].setdefault(symbol, (now_ms // 60_000) * 60_000)
        last = self._mark(symbol)
        if order_type == "market" or (side == "long" and last <= price) or (side == "short" and last >= price):
            self._fill_entry(oid, self.a["orders"][oid], last, now_ms, taker=True)
        self._save()
        return oid

    def order_state(self, symbol, order_id):
        od = self.a["orders"].get(order_id) or self.a.get("done_orders", {}).get(order_id)
        if od is None:
            return OrderState("canceled", 0.0, None)
        return OrderState(od.get("status", "open"), float(od.get("filled", 0.0)), od.get("average"))

    def cancel_order(self, symbol, order_id):
        od = self.a["orders"].pop(order_id, None)
        if od:
            od["status"] = "canceled"
            self.a.setdefault("done_orders", {})[order_id] = od
            self._save()

    def _conditional(self, symbol, side, qty, price, old_id, kind):
        last = float(self.a["last"].get(symbol) or self._mark(symbol))
        d = 1 if side == "long" else -1
        if kind == "sl" and d * (last - price) <= 0:
            raise StopWouldTrigger(f"stop {price} beyond market {last}")
        if old_id:
            self.a["orders"].pop(old_id, None)
        oid = f"P{kind.upper()}-" + uuid.uuid4().hex[:10]
        self.a["orders"][oid] = {"symbol": symbol, "type": kind, "side": side, "qty": qty,
                                 "price": self.round_price(symbol, price), "created_ms": int(time.time() * 1000)}
        self._save()
        return oid

    def set_stop_loss(self, symbol, side, qty, price, old_id):
        return self._conditional(symbol, side, qty, price, old_id, "sl")

    def set_take_profit(self, symbol, side, qty, price, old_id):
        return self._conditional(symbol, side, qty, price, old_id, "tp")

    def protective_ids(self, symbol):
        return {oid for oid, od in self.a["orders"].items() if od["symbol"] == symbol and od["type"] in ("sl", "tp")}

    def close_position(self, symbol, side, qty):
        if symbol not in self.a["positions"]:
            return None
        px = self._mark(symbol)
        self._close(symbol, px, int(time.time() * 1000), "MANUAL")
        self._save()
        return px

    def cancel_all(self, symbol):
        for oid, od in list(self.a["orders"].items()):
            if od["symbol"] == symbol:
                self.a["orders"].pop(oid)
        self._save()

    def close_result(self, symbol, since_ms):
        for c in reversed(self.a["closed"]):
            if c["symbol"] == symbol and c["ts"] >= since_ms - 60_000:
                return CloseResult(c["exit"], c["pnl"], c["fees"])
        return CloseResult(None, 0.0, 0.0)
