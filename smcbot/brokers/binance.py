"""Binance USDⓈ-M futures via ccxt (live or Demo Trading).

Since 2025-12-09 Binance serves STOP_MARKET / TAKE_PROFIT_MARKET through the Algo Order API;
ccxt (>= 4.5) routes orders created with ``stopLossPrice`` / ``takeProfitPrice`` there, and the
``{'trigger': True}`` flag makes cancel/fetch use the algo endpoints too.
"""
from __future__ import annotations

import logging

import ccxt

from .base import Broker, CloseResult, OrderState, PositionInfo, StopWouldTrigger

log = logging.getLogger(__name__)


class BinanceBroker(Broker):
    def __init__(self, api_key: str, secret: str, demo: bool, risk_cfg):
        if not api_key or not secret:
            raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET .env dosyasında tanımlı değil")
        self.cfg = risk_cfg
        self.exchange = ccxt.binanceusdm({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"adjustForTimeDifference": True},
        })
        if demo:
            self.exchange.enable_demo_trading(True)
        self.exchange.load_markets()
        self._check_position_mode()
        self._prepared: dict[str, int] = {}

    def _check_position_mode(self) -> None:
        try:
            mode = self.exchange.fetch_position_mode()
        except Exception as e:
            log.warning("Pozisyon modu okunamadı: %s", e)
            return
        if mode.get("hedged"):
            raise RuntimeError("Hesap Hedge modunda. Binance Futures > Ayarlar > Pozisyon Modu > One-way yapın.")

    # ------------------------------------------------------------------
    def _balance_info(self) -> dict:
        return self.exchange.fetch_balance().get("info", {})

    def equity(self) -> float:
        b = self.exchange.fetch_balance()
        info = b.get("info", {})
        if info.get("totalMarginBalance") is not None:
            return float(info["totalMarginBalance"])
        return float(b["total"].get("USDT", 0.0))

    def free_balance(self) -> float:
        b = self.exchange.fetch_balance()
        info = b.get("info", {})
        if info.get("availableBalance") is not None:
            return float(info["availableBalance"])
        return float(b["free"].get("USDT", 0.0))

    def positions(self) -> dict[str, PositionInfo]:
        out = {}
        for p in self.exchange.fetch_positions():
            qty = float(p.get("contracts") or 0)
            if qty <= 0:
                continue
            out[p["symbol"]] = PositionInfo(
                symbol=p["symbol"], side=p["side"], qty=qty,
                entry_price=float(p.get("entryPrice") or 0),
                unrealized_pnl=float(p.get("unrealizedPnl") or 0),
                mark_price=float(p["markPrice"]) if p.get("markPrice") else None,
                liquidation_price=float(p["liquidationPrice"]) if p.get("liquidationPrice") else None,
            )
        return out

    def prepare(self, symbol: str, leverage: int) -> None:
        if self._prepared.get(symbol) == leverage:
            return
        try:
            self.exchange.set_margin_mode(self.cfg.margin_mode, symbol)
        except ccxt.BaseError as e:
            if "No need to change" not in str(e):
                log.warning("%s margin modu ayarlanamadı: %s", symbol, e)
        lev = leverage
        while lev >= 1:
            try:
                self.exchange.set_leverage(lev, symbol)
                break
            except ccxt.BaseError as e:
                log.warning("%s kaldıraç %dx ayarlanamadı (%s), düşürülüyor", symbol, lev, e)
                lev -= 1
        self._prepared[symbol] = leverage

    # ------------------------------------------------------------------
    def place_entry(self, symbol, side, qty, price, client_id, order_type="limit"):
        order_side = "buy" if side == "long" else "sell"
        if order_type == "market":
            o = self.exchange.create_order(symbol, "market", order_side, qty, None, {"clientOrderId": client_id})
        else:
            o = self.exchange.create_order(symbol, "limit", order_side, qty, self.round_price(symbol, price),
                                           {"timeInForce": "GTC", "clientOrderId": client_id})
        return str(o["id"])

    def order_state(self, symbol, order_id):
        o = self.exchange.fetch_order(order_id, symbol)
        st = o.get("status")
        status = "filled" if st == "closed" else ("open" if st == "open" else "canceled")
        return OrderState(status, float(o.get("filled") or 0), float(o["average"]) if o.get("average") else None)

    def cancel_order(self, symbol, order_id):
        try:
            self.exchange.cancel_order(order_id, symbol)
        except ccxt.OrderNotFound:
            pass

    def _conditional(self, symbol, side, qty, price, old_id, kind: str) -> str:
        close_side = "sell" if side == "long" else "buy"
        params = {"reduceOnly": True}
        if kind == "sl":
            params.update(stopLossPrice=self.round_price(symbol, price), workingType=self.cfg.sl_working_type)
        else:
            params.update(takeProfitPrice=self.round_price(symbol, price), workingType="CONTRACT_PRICE")
        try:
            o = self.exchange.create_order(symbol, "market", close_side, qty, None, params)
        except ccxt.BaseError as e:
            if "-2021" in str(e) or "immediately trigger" in str(e).lower():
                raise StopWouldTrigger(str(e)) from e
            raise
        new_id = str(o["id"])
        if old_id:  # new order first, then remove the old one -> never unprotected
            self._cancel_trigger(symbol, old_id)
        return new_id

    def set_stop_loss(self, symbol, side, qty, price, old_id):
        return self._conditional(symbol, side, qty, price, old_id, "sl")

    def set_take_profit(self, symbol, side, qty, price, old_id):
        return self._conditional(symbol, side, qty, price, old_id, "tp")

    def _cancel_trigger(self, symbol, order_id):
        try:
            self.exchange.cancel_order(order_id, symbol, {"trigger": True})
        except ccxt.OrderNotFound:
            pass
        except ccxt.BaseError as e:
            log.warning("%s koşullu emir iptal edilemedi (%s): %s", symbol, order_id, e)

    def protective_ids(self, symbol):
        return {str(o["id"]) for o in self.exchange.fetch_open_orders(symbol, params={"trigger": True})}

    def close_position(self, symbol, side, qty):
        o = self.exchange.create_order(symbol, "market", "sell" if side == "long" else "buy", qty, None,
                                       {"reduceOnly": True})
        return float(o["average"]) if o.get("average") else None

    def cancel_all(self, symbol):
        for params in ({}, {"trigger": True}):
            try:
                self.exchange.cancel_all_orders(symbol, params)
            except ccxt.BaseError as e:
                log.debug("%s cancel_all %s: %s", symbol, params, e)

    def close_result(self, symbol, since_ms):
        trades = self.exchange.fetch_my_trades(symbol, since=since_ms, limit=500)
        pnl = fees = 0.0
        qty = notional = 0.0
        for t in trades:
            info = t.get("info", {})
            rp = float(info.get("realizedPnl") or 0)
            pnl += rp
            fees += float((t.get("fee") or {}).get("cost") or 0)
            if rp != 0:
                qty += float(t["amount"])
                notional += float(t["amount"]) * float(t["price"])
        return CloseResult(notional / qty if qty else None, pnl - fees, fees)
