"""PostgreSQL journal: trades, their reasons, every optimization event, rejected setups, scans,
equity snapshots and logs. Falls back to CSV when no database is configured."""
from __future__ import annotations

import csv
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..trade import Trade
from ..utils import from_iso

log = logging.getLogger(__name__)

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    psycopg = None
    Jsonb = None

SCHEMA = Path(__file__).with_name("schema.sql")


def _j(value):
    return Jsonb(value) if Jsonb is not None else value


class Journal:
    def __init__(self, dsn: str, mode: str, csv_dir: str = "state"):
        self.dsn = dsn
        self.mode = mode
        self.conn = None
        self.lock = threading.RLock()
        self.csv_path = Path(csv_dir) / "trades.csv"
        if dsn and psycopg is not None:
            self._connect()

    # ------------------------------------------------------------------
    def _connect(self) -> None:
        try:
            self.conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
            with self.conn.cursor() as cur:
                cur.execute(SCHEMA.read_text(encoding="utf-8"))
            log.info("PostgreSQL bağlandı, şema hazır")
        except Exception as e:
            log.warning("PostgreSQL'e bağlanılamadı: %s (işlemler CSV'ye yazılacak)", e)
            self.conn = None

    @property
    def enabled(self) -> bool:
        return self.conn is not None

    def _exec(self, sql: str, params=None, fetch: bool = False):
        if not self.dsn or psycopg is None:
            return None
        with self.lock:
            for attempt in range(2):
                if self.conn is None or self.conn.closed:
                    self._connect()
                    if self.conn is None:
                        return None
                try:
                    with self.conn.cursor() as cur:
                        cur.execute(sql, params)
                        if fetch:
                            cols = [c.name for c in cur.description]
                            return [dict(zip(cols, row)) for row in cur.fetchall()]
                        return None
                except psycopg.OperationalError as e:
                    log.warning("DB bağlantısı koptu (%s), yeniden bağlanılıyor", e)
                    self.conn = None
                except Exception as e:
                    log.error("DB hatası: %s", e)
                    return None
        return None

    def query(self, sql: str, params=None) -> list[dict]:
        return self._exec(sql, params, fetch=True) or []

    # ------------------------------------------------------------------
    def upsert_trade(self, t: Trade, mode: str | None = None) -> None:
        filled = from_iso(t.filled_at)
        closed = from_iso(t.closed_at)
        hold = (closed - filled).total_seconds() / 60 if filled and closed else None
        mfe_r = t.r_at(t.mfe_price) if t.mfe_price and t.fill_price else None
        mae_r = t.r_at(t.mae_price) if t.mae_price and t.fill_price else None
        row = dict(
            id=t.id, mode=mode or self.mode, strategy=t.strategy, symbol=t.symbol, side=t.side, status=t.status, setup_id=t.setup_id,
            score=t.score, reasons=_j(t.reasons), poi=t.poi, swept=_j(t.swept), planned_entry=t.entry,
            fill_price=t.fill_price, initial_sl=t.initial_sl, initial_tp=t.initial_tp, sl=t.sl, tp=t.tp,
            tp_r=t.tp_r, qty=t.filled_qty or t.qty, leverage=t.leverage, risk_pct=t.risk_pct,
            risk_usd=t.risk_usd, created_at=from_iso(t.created_at), filled_at=filled, closed_at=closed,
            exit_price=t.exit_price, exit_reason=t.exit_reason, pnl=t.pnl, fees=t.fees,
            r_multiple=t.r_multiple, mfe_r=mfe_r, mae_r=mae_r, hold_minutes=hold,
            be_done=t.be_done, tp_extended=t.tp_extended, setup=_j(t.setup),
        )
        if self.dsn and psycopg is not None:
            cols = list(row)
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
            sql = (f"INSERT INTO trades ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)}) "
                   f"ON CONFLICT (id) DO UPDATE SET {updates}, updated_at = now()")
            self._exec(sql, row)
        if not self.enabled and t.status in ("closed", "cancelled"):
            self._csv(row)

    def _csv(self, row: dict) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.csv_path.exists()
        plain = {k: (getattr(v, "obj", v)) for k, v in row.items()}
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(plain))
            if new:
                w.writeheader()
            w.writerow(plain)

    def add_event(self, trade_id: str, event: str, message: str, price: float | None = None,
                  data: dict | None = None) -> None:
        self._exec("INSERT INTO trade_events (trade_id, event, message, price, data) VALUES (%s, %s, %s, %s, %s)",
                   (trade_id, event, message, price, _j(data or {})))

    def record_setup(self, s, taken: bool, reject_reason: str | None, mode: str | None = None) -> None:
        self._exec(
            """INSERT INTO setups (id, mode, strategy, symbol, side, created_at, score, taken, reject_reason, entry,
                                   sl, tp, tp_r, poi, swept, reasons, features)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET
                   taken = setups.taken OR EXCLUDED.taken,
                   reject_reason = CASE WHEN setups.taken OR EXCLUDED.taken THEN NULL ELSE EXCLUDED.reject_reason END,
                   score = EXCLUDED.score""",
            (s.id, mode or self.mode, s.strategy, s.symbol, s.side, s.created_at.to_pydatetime(), s.score, taken,
             reject_reason,
             s.entry, s.sl, s.tp, s.tp_r, s.poi, _j(s.swept), _j(s.reasons), _j(s.features)),
        )

    def record_scan(self, duration_ms: int, symbols: int, found: int, placed: int,
                    blocked_reason: str | None, details: dict, mode: str | None = None) -> None:
        self._exec(
            "INSERT INTO scan_runs (mode, duration_ms, symbols, setups_found, orders_placed, blocked_reason, details) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (mode or self.mode, duration_ms, symbols, found, placed, blocked_reason, _j(details)),
        )

    def snapshot_equity(self, equity: float, free: float, open_positions: int, open_risk: float,
                        mode: str | None = None) -> None:
        self._exec("INSERT INTO equity_snapshots (mode, equity, free_balance, open_positions, open_risk_usd) "
                   "VALUES (%s,%s,%s,%s,%s)", (mode or self.mode, equity, free, open_positions, open_risk))

    def insert_logs(self, rows: list[tuple]) -> None:
        if not rows or not self.enabled:
            return
        with self.lock:
            try:
                with self.conn.cursor() as cur:
                    cur.executemany("INSERT INTO bot_logs (ts, level, logger, message, context) VALUES (%s,%s,%s,%s,%s)",
                                    [(ts, lvl, name, msg, _j(ctx)) for ts, lvl, name, msg, ctx in rows])
            except Exception:
                self.conn = None


class DBLogHandler(logging.Handler):
    """Ships log records to PostgreSQL in batches from a background thread."""

    SKIP = ("psycopg", "smcbot.storage.db", "urllib3", "ccxt")

    def __init__(self, journal: Journal, level=logging.INFO):
        super().__init__(level)
        self.journal = journal
        self.q: queue.Queue = queue.Queue(maxsize=10_000)
        threading.Thread(target=self._run, daemon=True, name="db-log").start()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(self.SKIP):
            return
        try:
            ctx = {"module": record.module, "line": record.lineno}
            if record.exc_info:
                ctx["exception"] = logging.Formatter().formatException(record.exc_info)
            self.q.put_nowait((datetime.fromtimestamp(record.created, tz=timezone.utc), record.levelname,
                               record.name, record.getMessage(), ctx))
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            batch = [self.q.get()]
            try:
                while len(batch) < 200:
                    batch.append(self.q.get(timeout=2))
            except queue.Empty:
                pass
            self.journal.insert_logs(batch)
