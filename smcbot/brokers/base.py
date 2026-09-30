from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import ccxt


@dataclass
class PositionInfo:
    symbol: str
    side: str
    qty: float
    entry_price: float
    unrealized_pnl: float = 0.0
    mark_price: float | None = None
    liquidation_price: float | None = None


@dataclass
class OrderState:
    status: str          # open | filled | canceled
    filled: float
    average: float | None


@dataclass
class MarketRules:
    qty_step: float
    price_step: float
    min_qty: float
    min_notional: float


@dataclass
class CloseResult:
    exit_price: float | None
    pnl: float
    fees: float


class StopWouldTrigger(Exception):
    """The requested stop is already beyond the market price."""


class Broker(ABC):
    exchange: ccxt.Exchange

    def sync(self) -> None:
        """Process simulated fills (paper) - no-op for real exchanges."""

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def free_balance(self) -> float: ...

    @abstractmethod
    def positions(self) -> dict[str, PositionInfo]: ...

    @abstractmethod
    def prepare(self, symbol: str, leverage: int) -> None: ...

    @abstractmethod
    def place_entry(self, symbol: str, side: str, qty: float, price: float, client_id: str,
                    order_type: str = "limit") -> str: ...

    @abstractmethod
    def order_state(self, symbol: str, order_id: str) -> OrderState: ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str) -> None: ...

    @abstractmethod
    def set_stop_loss(self, symbol: str, side: str, qty: float, price: float, old_id: str | None) -> str: ...

    @abstractmethod
    def set_take_profit(self, symbol: str, side: str, qty: float, price: float, old_id: str | None) -> str: ...

    @abstractmethod
    def protective_ids(self, symbol: str) -> set[str]: ...

    @abstractmethod
    def close_position(self, symbol: str, side: str, qty: float) -> float | None: ...

    @abstractmethod
    def cancel_all(self, symbol: str) -> None: ...

    @abstractmethod
    def close_result(self, symbol: str, since_ms: int) -> CloseResult: ...

    # ---- helpers shared by all brokers (market metadata via ccxt) ----
    def rules(self, symbol: str) -> MarketRules:
        m = self.exchange.market(symbol)
        lim = m.get("limits", {})
        return MarketRules(
            qty_step=float(m["precision"]["amount"]),
            price_step=float(m["precision"]["price"]),
            min_qty=float((lim.get("amount") or {}).get("min") or 0),
            min_notional=float((lim.get("cost") or {}).get("min") or 5),
        )

    def round_qty(self, symbol: str, qty: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(symbol, qty))
        except Exception:
            return 0.0

    def round_price(self, symbol: str, price: float) -> float:
        return float(self.exchange.price_to_precision(symbol, price))

    def last_price(self, symbol: str) -> float:
        return float(self.exchange.fetch_ticker(symbol)["last"])
