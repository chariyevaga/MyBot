from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

from .utils import from_iso, now_utc, to_iso


@dataclass
class Trade:
    """A bot-managed trade (pending limit order -> open position -> closed)."""

    id: str
    symbol: str
    side: str                  # long | short
    status: str                # pending | open | closed | cancelled
    setup_id: str
    score: int
    reasons: list
    poi: str
    swept: list
    entry: float               # planned (limit) entry
    sl: float                  # current stop
    tp: float | None           # current take profit (None = trailing stop only)
    initial_sl: float
    initial_tp: float | None
    tp_r: float
    qty: float
    risk_pct: float
    risk_usd: float
    leverage: int
    created_at: str
    expires_at: str
    cancel_price: float | None  # cancel the pending order if price reaches this before filling
    targets: list = field(default_factory=list)
    strategy: str = "smc"      # smc | trend
    account: str = "main"      # main (paper/demo/live) | shadow (always virtual, observation)
    setup: dict = field(default_factory=dict)
    entry_order_id: str | None = None
    entry_open: bool = True
    sl_order_id: str | None = None
    tp_order_id: str | None = None
    protected_qty: float = 0.0
    filled_at: str | None = None
    fill_price: float | None = None
    filled_qty: float = 0.0
    be_done: bool = False
    tp_extended: bool = False
    mfe_price: float | None = None
    mae_price: float | None = None
    closed_at: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    fees: float = 0.0
    r_multiple: float | None = None
    log: list = field(default_factory=list)

    @property
    def dir(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def open_price(self) -> float:
        return self.fill_price if self.fill_price else self.entry

    @property
    def risk_unit(self) -> float:
        """Initial risk per unit (1R) measured from the actual fill."""
        return abs(self.open_price - self.initial_sl)

    def r_at(self, price: float) -> float:
        ru = self.risk_unit
        return self.dir * (price - self.open_price) / ru if ru > 0 else 0.0

    def price_at_r(self, r: float) -> float:
        return self.open_price + self.dir * r * self.risk_unit

    def current_risk_usd(self) -> float:
        """Money still at risk if the current stop is hit (<= 0 once the stop is in profit)."""
        qty = self.filled_qty or self.qty
        return max(0.0, self.dir * (self.open_price - self.sl) * qty)

    def add_log(self, event: str, message: str) -> None:
        self.log.append({"ts": to_iso(now_utc()), "event": event, "message": message})
        self.log = self.log[-50:]

    @property
    def filled_ts(self):
        return from_iso(self.filled_at)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
