"""Live engine.

Every ``scan_interval_minutes`` (5), right after the candle closes:
    1. housekeeping (equity, risk breakers, news calendar, expired pending orders)
    2. SCAN      - look for new entries on all coins (only coins without a position/order)
    3. OPTIMIZE  - position optimization for every open position
Between cycles a fast monitor loop (15 s) detects fills and closes so that a new position gets
its stop-loss / take-profit within seconds.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path

import ccxt
import pandas as pd

from . import messages as M
from .brokers.base import Broker, StopWouldTrigger
from .brokers.binance import BinanceBroker
from .brokers.paper import PaperBroker
from .market_data import MarketData
from .news import NewsCalendar
from .optimizer import Action, build_context, optimize
from .risk import RiskGuard, choose_leverage, position_size, risk_pct_for_score
from .storage.db import DBLogHandler, Journal
from .storage.kv import make_kv
from .strategy import Prepared, Setup, find_setups, prepare
from .telegram import Telegram
from .trade import Trade
from .utils import base_of, ccxt_symbol, fmt_price, fmt_usd, from_iso, now_utc, to_iso, ts_ns

log = logging.getLogger("smcbot.engine")

SETUP_TTL = 24 * 3600


def make_broker(cfg, kv) -> Broker:
    if cfg.mode == "paper":
        ex = ccxt.binanceusdm({"enableRateLimit": True})
        ex.load_markets()
        return PaperBroker(MarketData(ex), kv, cfg.paper.start_balance, cfg.risk.fees)
    return BinanceBroker(cfg.secrets.binance_key, cfg.secrets.binance_secret, cfg.mode == "demo", cfg.risk)


def resolve_universe(ex: ccxt.Exchange, cfg) -> list[str]:
    u = cfg.universe
    if not ex.markets:
        ex.load_markets()
    if u.mode == "top_volume":
        tickers = ex.fetch_tickers()
        min_onboard = (time.time() - u.min_listing_days * 86400) * 1000
        rows = []
        for sym, m in ex.markets.items():
            info = m.get("info", {})
            if not (m.get("swap") and m.get("linear") and m.get("active") and m.get("quote") == cfg.quote):
                continue
            if info.get("underlyingType") != "COIN" or info.get("contractType") != "PERPETUAL":
                continue
            if m["base"] in u.exclude or float(info.get("onboardDate") or 0) > min_onboard:
                continue
            rows.append((sym, float((tickers.get(sym) or {}).get("quoteVolume") or 0)))
        return [s for s, _ in sorted(rows, key=lambda x: -x[1])[: u.top_n]]
    out = []
    for base in u.symbols:
        sym = ccxt_symbol(base, cfg.quote)
        if sym in ex.markets:
            out.append(sym)
        else:
            log.warning("%s Binance futures'ta bulunamadı, atlandı", sym)
    return out


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tz = cfg.telegram.get("display_timezone", "UTC")
        self.kv = make_kv(cfg.secrets.redis_url, cfg.storage.redis_prefix, cfg.storage.state_file)
        self.journal = Journal(cfg.secrets.database_url, cfg.mode)
        if self.journal.enabled:
            logging.getLogger().addHandler(DBLogHandler(self.journal, cfg.storage.db_log_level))
        self.broker = make_broker(cfg, self.kv)
        self.md = MarketData(self.broker.exchange)
        self.symbols = resolve_universe(self.broker.exchange, cfg)
        self.news = NewsCalendar(cfg.news, self.kv)
        self.tg = Telegram(cfg.secrets.telegram_token, cfg.secrets.telegram_chat_id, cfg.telegram.enabled, self.kv)
        self.risk = RiskGuard(cfg.risk, self.kv)
        self.lock = threading.RLock()
        self.trades: dict[str, Trade] = {k: Trade.from_dict(v) for k, v in (self.kv.get_json("trades") or {}).items()}
        self._stop = threading.Event()
        self._cache: dict[str, dict] = {}
        self._last_snapshot = 0.0
        self.next_cycle: pd.Timestamp | None = None
        self.last_scan: dict = {}

    # ------------------------------------------------------------------ persistence
    def _active(self, status: str | None = None) -> list[Trade]:
        return [t for t in self.trades.values()
                if t.status in (("pending", "open") if status is None else (status,))]

    def _save(self) -> None:
        self.trades = {k: t for k, t in self.trades.items() if t.status in ("pending", "open")}
        self.kv.set_json("trades", {k: t.to_dict() for k, t in self.trades.items()})

    def _persist(self, t: Trade, event: str | None = None, message: str = "", price: float | None = None,
                 data: dict | None = None) -> None:
        if event:
            t.add_log(event, message)
            self.journal.add_event(t.id, event, message, price, data)
        self.journal.upsert_trade(t)
        self._save()

    # ------------------------------------------------------------------ main loop
    def run(self, once: bool = False) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except ValueError:
                pass
        self.news.refresh()  # the Redis cache survives restarts; the feed rate-limits repeated downloads
        eq = self.broker.equity()
        log.info("Bot başladı | mod=%s | coinler=%s | bakiye=%.2f | state=%s | db=%s",
                 self.cfg.mode, ",".join(base_of(s) for s in self.symbols), eq, self.kv.backend,
                 "postgres" if self.journal.enabled else "csv")
        self.tg.send(
            f"🤖 <b>SMC Bot başladı</b> ({self.cfg.mode.upper()})\n"
            f"Bakiye: {fmt_usd(eq)}\nCoinler: {', '.join(base_of(s) for s in self.symbols)}\n"
            f"Aktif işlem: {len(self._active())}\n/help ile komutlar"
        )
        self._warn_unmanaged()
        if once:  # single scan + optimize cycle (testing / cron usage)
            with self.lock:
                self.monitor()
                self.cycle()
            self._heartbeat()
            return
        if self.cfg.telegram.commands:
            self.tg.start_commands(self.handle_command)
        self.next_cycle = self._next_cycle_time(now_utc())
        while not self._stop.is_set():
            try:
                with self.lock:
                    self.monitor()
            except Exception:
                log.exception("Monitor hatası")
            if now_utc() >= self.next_cycle:
                try:
                    with self.lock:
                        self.cycle()
                except Exception as e:
                    log.exception("Döngü hatası")
                    self.tg.send(f"⚠️ Döngü hatası: {e}")
                self.next_cycle = self._next_cycle_time(now_utc())
            self._heartbeat()
            self._stop.wait(self.cfg.schedule.monitor_interval_seconds)
        self.tg.stop()
        self.tg.send("⏹ SMC Bot durduruldu. Açık pozisyonların SL/TP emirleri borsada duruyor.")
        log.info("Bot durduruldu")

    def _next_cycle_time(self, now: pd.Timestamp) -> pd.Timestamp:
        step = self.cfg.schedule.scan_interval_minutes
        return now.floor(f"{step}min") + pd.Timedelta(minutes=step) + pd.Timedelta(
            seconds=self.cfg.schedule.candle_close_delay_seconds)

    def _heartbeat(self) -> None:
        now = now_utc()
        self.kv.set_json("heartbeat", to_iso(now), ttl=900)
        try:
            Path("state").mkdir(exist_ok=True)
            Path("state/heartbeat").write_text(to_iso(now))
        except OSError:
            pass

    def _warn_unmanaged(self) -> None:
        try:
            managed = {t.symbol for t in self._active()}
            other = [p for s, p in self.broker.positions().items() if s not in managed]
            if other:
                self.tg.send("ℹ️ Bot tarafından yönetilmeyen pozisyonlar var (dokunulmayacak): "
                             + ", ".join(f"{base_of(p.symbol)} {p.side}" for p in other))
        except Exception as e:
            log.warning("Pozisyon kontrolü yapılamadı: %s", e)

    # ------------------------------------------------------------------ fast monitor
    def monitor(self) -> None:
        self.broker.sync()
        active = self._active()
        if not active:
            return
        positions = self.broker.positions()
        for t in active:
            try:
                if t.status == "pending":
                    self._check_pending(t, positions)
                elif t.status == "open":
                    self._check_open(t, positions)
            except Exception:
                log.exception("%s kontrol hatası", t.symbol)

    def _check_pending(self, t: Trade, positions) -> None:
        st = self.broker.order_state(t.symbol, t.entry_order_id)
        pos = positions.get(t.symbol)
        qty = pos.qty if (pos and pos.side == t.side) else st.filled
        if qty > 0:
            t.status = "open"
            t.filled_at = to_iso(now_utc())
            t.fill_price = (pos.entry_price if pos else None) or st.average or t.entry
            t.filled_qty = qty
            t.mfe_price = t.mae_price = t.fill_price
            t.entry_open = st.status == "open"
            self._protect(t, qty)
            if t.status == "open":
                self._persist(t, "FILLED", f"Giriş doldu @ {fmt_price(t.fill_price)} x{qty:g}", t.fill_price)
                self.tg.send(M.filled(t))
        elif st.status == "canceled":
            self._finish_cancel(t, "Emir borsada iptal edildi")

    def _check_open(self, t: Trade, positions) -> None:
        pos = positions.get(t.symbol)
        if t.entry_open:
            st = self.broker.order_state(t.symbol, t.entry_order_id)
            if st.status != "open":
                t.entry_open = False
        if pos is None or pos.qty <= 0 or pos.side != t.side:
            if t.entry_open:
                self.broker.cancel_order(t.symbol, t.entry_order_id)
                t.entry_open = False
            self._finalize(t)
            return
        if abs(pos.qty - t.protected_qty) > 1e-12:
            t.filled_qty = pos.qty
            t.fill_price = pos.entry_price or t.fill_price
            self._protect(t, pos.qty)
            self._persist(t, "QTY_CHANGED", f"Pozisyon miktarı {pos.qty:g}, SL/TP güncellendi")

    def _protect(self, t: Trade, qty: float) -> None:
        """Place / resize SL and TP. A position without a stop is closed immediately."""
        try:
            t.sl_order_id = self.broker.set_stop_loss(t.symbol, t.side, qty, t.sl, t.sl_order_id)
        except StopWouldTrigger:
            self._close_now(t, "Stop seviyesi fiyatın gerisinde kaldı (koruma)")
            return
        except Exception as e:
            log.exception("%s SL konulamadı", t.symbol)
            self.tg.send(f"🚨 {base_of(t.symbol)} STOP KONULAMADI ({e}). Pozisyon güvenlik için kapatılıyor.")
            self._close_now(t, "SL konulamadı (güvenlik kapanışı)")
            return
        try:
            t.tp_order_id = self.broker.set_take_profit(t.symbol, t.side, qty, t.tp, t.tp_order_id)
        except Exception as e:
            log.warning("%s TP konulamadı: %s (SL aktif, tekrar denenecek)", t.symbol, e)
        t.protected_qty = qty

    def _close_now(self, t: Trade, reason: str) -> None:
        qty = t.filled_qty or t.qty
        try:
            self.broker.close_position(t.symbol, t.side, qty)
        except Exception as e:
            log.exception("%s kapatılamadı", t.symbol)
            self.tg.send(f"🚨 {base_of(t.symbol)} pozisyonu KAPATILAMADI: {e}. Lütfen manuel kontrol edin!")
            return
        time.sleep(1.0)
        self._finalize(t, reason)

    def _finalize(self, t: Trade, reason: str | None = None) -> None:
        now = now_utc()
        since_ms = int(ts_ns(from_iso(t.created_at)) // 1_000_000)
        try:
            res = self.broker.close_result(t.symbol, since_ms)
        except Exception as e:
            log.warning("%s kapanış sonucu okunamadı: %s", t.symbol, e)
            res = None
        self.broker.cancel_all(t.symbol)
        exit_px = (res.exit_price if res else None) or self._price(t.symbol) or t.sl
        if reason is None:
            if abs(exit_px - t.tp) < abs(exit_px - t.sl):
                reason = "TP" + (" (uzatılmış)" if t.tp_extended else "")
            elif t.r_at(t.sl) < -0.2:
                reason = "SL"
            elif t.r_at(t.sl) <= 0.2:
                reason = "Break-even stop"
            else:
                reason = "Kâr kilidi / trailing stop"
        t.status = "closed"
        t.closed_at = to_iso(now)
        t.exit_price = float(exit_px)
        t.exit_reason = reason
        t.r_multiple = round(t.r_at(exit_px), 3)
        if res is not None:
            t.pnl, t.fees = round(res.pnl, 4), round(res.fees, 4)
        else:
            t.pnl = t.dir * (exit_px - t.open_price) * (t.filled_qty or t.qty)
        breaker = self.risk.register_close(t.pnl or 0.0, now)
        self.kv.set_json(f"cooldown:{t.symbol}", 1, ttl=int(self.cfg.risk.symbol_cooldown_minutes * 60))
        self._persist(t, "CLOSED", f"{reason} @ {fmt_price(exit_px)} | PnL {t.pnl:+.2f} ({t.r_multiple:+.2f}R)", exit_px)
        log.info("%s %s kapandı: %s PnL=%.2f R=%.2f", t.symbol, t.side, reason, t.pnl or 0, t.r_multiple)
        self.tg.send(M.closed(t))
        if breaker:
            self.tg.send("⛔ " + breaker)

    def _finish_cancel(self, t: Trade, reason: str) -> None:
        t.status = "cancelled"
        t.exit_reason = reason
        t.closed_at = to_iso(now_utc())
        self._persist(t, "CANCELLED", reason)
        self.tg.send(M.cancelled(t, reason))

    # ------------------------------------------------------------------ 5-minute cycle
    def cycle(self) -> None:
        t0 = time.time()
        now = now_utc()
        self._cache = {}
        equity = self.broker.equity()
        self.risk.on_cycle(equity, now)
        self.news.refresh()
        self._manage_pending(now)
        self.scan(now, equity)        # 1) new entries
        self.optimize_positions(now)  # 2) position optimization
        if time.time() - self._last_snapshot >= self.cfg.storage.equity_snapshot_minutes * 60:
            self._last_snapshot = time.time()
            open_risk = sum(t.current_risk_usd() for t in self._active("open"))
            self.journal.snapshot_equity(equity, self.broker.free_balance(), len(self._active("open")), open_risk)
        self._save()
        log.info("Döngü bitti (%.1fs) | açık=%d bekleyen=%d", time.time() - t0,
                 len(self._active("open")), len(self._active("pending")))

    def _prepared(self, symbol: str) -> tuple[Prepared, pd.DataFrame]:
        if symbol not in self._cache:
            frames = {
                "ltf": self.md.candles(symbol, self.cfg.timeframes.ltf, 700),
                "mtf": self.md.candles(symbol, self.cfg.timeframes.mtf, 600),
                "htf": self.md.candles(symbol, self.cfg.timeframes.htf, 300),
            }
            exec_df = self.md.candles(symbol, self.cfg.timeframes.exec, 300)
            self._cache[symbol] = {"P": prepare(frames, self.cfg), "exec": exec_df}
        c = self._cache[symbol]
        return c["P"], c["exec"]

    def _price(self, symbol: str) -> float | None:
        try:
            return self.md.last_price(symbol)
        except Exception:
            return None

    def _manage_pending(self, now: pd.Timestamp) -> None:
        for t in self._active("pending"):
            price = self._price(t.symbol)
            reason = None
            if now >= from_iso(t.expires_at):
                reason = "Süre doldu, fiyat giriş bölgesine gelmedi"
            elif price is not None and t.dir * (price - t.cancel_price) >= 0:
                reason = "Fiyat dolmadan hedefe gitti"
            if reason:
                self.broker.cancel_order(t.symbol, t.entry_order_id)
                self._check_pending(t, self.broker.positions())  # a last-second fill turns it into a position
                if t.status == "pending":
                    self._finish_cancel(t, reason)
        for t in self._active("open"):
            if t.entry_open and now >= from_iso(t.expires_at):
                self.broker.cancel_order(t.symbol, t.entry_order_id)
                t.entry_open = False
                self._persist(t, "ENTRY_REMAINDER_CANCELLED", "Kısmi dolum: kalan emir iptal edildi")

    # ------------------------------------------------------------------ scan
    def _block_reason(self, now: pd.Timestamp) -> str | None:
        ok, reason = self.risk.can_open(now)
        if not ok:
            return reason
        if self.kv.get_json("paused"):
            return "Kullanıcı durdurdu (/pause)"
        ev = self.news.blocking_event(now)
        if ev:
            if ev.get("country") == "-":
                return ev["title"]
            return f"Haber filtresi: {ev['title']} ({ev['country']}) {M.local(from_iso(ev['time']), self.tz)}"
        return None

    def scan(self, now: pd.Timestamp, equity: float) -> None:
        t0 = time.time()
        blocked = self._block_reason(now)
        max_age = pd.Timedelta(minutes=self.cfg.strategy.setup_max_age_minutes)
        start_ns = ts_ns(now - max_age)

        prepared: dict[str, Prepared] = {}
        for sym in self.symbols:
            try:
                prepared[sym] = self._prepared(sym)[0]
            except Exception as e:
                log.warning("%s veri alınamadı: %s", sym, e)
        btc, eth = ccxt_symbol("BTC", self.cfg.quote), ccxt_symbol("ETH", self.cfg.quote)

        candidates: list[Setup] = []
        for sym, P in prepared.items():
            ref = prepared.get(eth if sym == btc else btc)
            try:
                for s in find_setups(sym, P, self.cfg, ref, start_ns):
                    if s.created_at <= now and not self.kv.exists(f"setup:{s.id}"):
                        candidates.append(s)
            except Exception:
                log.exception("%s analiz hatası", sym)
        candidates.sort(key=lambda s: -s.score)

        active = self._active()
        occupied = {t.symbol for t in active}
        try:
            occupied |= set(self.broker.positions())
            free = self.broker.free_balance()
        except Exception as e:
            log.warning("Hesap bilgisi alınamadı: %s", e)
            free = 0.0
            blocked = blocked or "Hesap bilgisi alınamadı"
        slots = self.cfg.risk.max_open_positions - len(active)
        placed = 0
        for s in candidates:
            final = False
            if blocked:
                reject = blocked
            elif s.symbol in occupied:
                reject = "Bu coinde zaten pozisyon/emir var"
            elif self.kv.exists(f"cooldown:{s.symbol}"):
                reject = "Coin bekleme süresinde (son işlem yeni kapandı)"
            elif slots <= 0:
                reject = f"Maksimum pozisyon sayısı ({self.cfg.risk.max_open_positions}) dolu"
            else:
                reject, final = self._validate(s, now)
                if reject is None:
                    reject, final = self._open(s, equity, free, slots)
                    if reject is None:
                        placed += 1
                        slots -= 1
                        occupied.add(s.symbol)
            if reject is None or final:
                self.kv.set_json(f"setup:{s.id}", reject or "taken", ttl=SETUP_TTL)
            self.journal.record_setup(s, reject is None, reject)
            if reject:
                log.info("Setup atlandı %s skor=%d: %s", s.id, s.score, reject)

        self.last_scan = {"time": to_iso(now), "found": len(candidates), "placed": placed, "blocked": blocked,
                          "symbols": len(prepared)}
        self.journal.record_scan(int((time.time() - t0) * 1000), len(prepared), len(candidates), placed, blocked,
                                 {"candidates": [{"id": s.id, "score": s.score} for s in candidates[:20]]})
        log.info("Tarama: %d coin, %d yeni setup, %d emir%s", len(prepared), len(candidates), placed,
                 f" | engel: {blocked}" if blocked else "")

    def _validate(self, s: Setup, now: pd.Timestamp) -> tuple[str | None, bool]:
        d = s.dir
        if self.cfg.strategy.entry_mode == "market":
            limit = pd.Timedelta(minutes=self.cfg.schedule.scan_interval_minutes * 2)
            if now - s.created_at > limit:
                return "Setup kaçırıldı (market girişi için çok eski)", True
            price = self._price(s.symbol)
            if price is None:
                return "Fiyat alınamadı", False
            if d * (price - s.sl) <= 0 or d * (price - s.tp) >= 0:
                return "Fiyat SL/TP bölgesinin dışında", True
            if d * (price - s.entry) > self.cfg.strategy.max_entry_drift_r * s.risk:
                return f"Fiyat kaçtı (> {self.cfg.strategy.max_entry_drift_r}R)", True
            return None, False
        if now >= s.expires_at:
            return "Setup süresi doldu", True
        _, ex = self._prepared(s.symbol)
        since = ex[ex.index >= s.created_at]
        if not since.empty:
            worst = since["low"].min() if d == 1 else since["high"].max()
            best = since["high"].max() if d == 1 else since["low"].min()
            if d * (worst - s.entry) <= 0:
                return "Giriş bölgesi tarama öncesinde test edildi", True
            if d * (best - s.tp) >= 0:
                return "Fiyat girişten önce hedefe gitti", True
        price = self._price(s.symbol)
        if price is None:
            return "Fiyat alınamadı", False
        if d * (price - s.entry) <= 0:
            return "Fiyat giriş seviyesinin gerisinde", True
        return None, False

    def _open(self, s: Setup, equity: float, free: float, slots: int) -> tuple[str | None, bool]:
        rc = self.cfg.risk
        risk_pct = risk_pct_for_score(s.score, rc)
        if risk_pct <= 0:
            return "Skor risk basamaklarının altında", True
        open_risk = sum(t.current_risk_usd() for t in self._active("open")) + \
            sum(t.risk_usd for t in self._active("pending"))
        room = equity * rc.max_total_risk_pct / 100 - open_risk
        if room < equity * risk_pct / 100:
            if room < equity * 0.005:
                return f"Toplam açık risk limiti (%{rc.max_total_risk_pct}) dolu", False
            risk_pct = room / equity * 100
        qty, _ = position_size(equity, s.entry, s.sl, risk_pct, rc.fees)
        plan = choose_leverage(qty * s.entry, free, slots, abs(s.entry - s.sl) / s.entry, rc.leverage_max)
        qty = self.broker.round_qty(s.symbol, qty * plan.qty_scale)
        rules = self.broker.rules(s.symbol)
        if qty <= 0 or qty < rules.min_qty or qty * s.entry < rules.min_notional:
            return f"Pozisyon borsa minimumunun altında (min {rules.min_notional:g} USDT)", True
        risk_usd = qty * (abs(s.entry - s.sl) + s.entry * (rc.fees.maker + rc.fees.taker))
        entry = self.broker.round_price(s.symbol, s.entry)
        sl = self.broker.round_price(s.symbol, s.sl)
        tp = self.broker.round_price(s.symbol, s.tp)
        try:
            self.broker.prepare(s.symbol, plan.leverage)
            tid = f"{base_of(s.symbol)}-{s.created_at:%Y%m%d%H%M}-{s.side[0].upper()}"
            oid = self.broker.place_entry(s.symbol, s.side, qty, entry, ("smc" + tid.replace("-", ""))[:36],
                                          "market" if self.cfg.strategy.entry_mode == "market" else "limit")
        except Exception as e:
            log.exception("%s emir gönderilemedi", s.symbol)
            self.tg.send(f"⚠️ {base_of(s.symbol)} emir gönderilemedi: {e}")
            return f"Emir hatası: {e}", False
        t = Trade(
            id=tid, symbol=s.symbol, side=s.side, status="pending", setup_id=s.id, score=s.score,
            reasons=s.reasons, poi=s.poi, swept=s.swept, entry=entry, sl=sl, tp=tp, initial_sl=sl,
            initial_tp=tp, tp_r=s.tp_r, qty=qty, risk_pct=round(risk_usd / equity * 100, 3),
            risk_usd=round(risk_usd, 4), leverage=plan.leverage, created_at=to_iso(now_utc()),
            expires_at=to_iso(s.expires_at), cancel_price=tp, targets=s.targets, setup=s.to_dict(),
            entry_order_id=oid,
        )
        self.trades[t.id] = t
        kind = "Market" if self.cfg.strategy.entry_mode == "market" else "Limit"
        self._persist(t, "ORDER_PLACED", f"{kind} emir @ {fmt_price(entry)} (skor {s.score})", entry, s.to_dict())
        self.tg.send(M.order_placed(t, self.tz, kind.lower()))
        log.info("Emir: %s %s @ %s SL %s TP %s qty %s risk %.2f%%", s.symbol, s.side, entry, sl, tp, qty, t.risk_pct)
        try:  # protect a market fill right away instead of waiting for the monitor loop
            self.broker.sync()
            self._check_pending(t, self.broker.positions())
        except Exception:
            log.exception("%s ilk koruma kontrolü", s.symbol)
        return None, True

    # ------------------------------------------------------------------ optimization
    def optimize_positions(self, now: pd.Timestamp) -> None:
        open_trades = self._active("open")
        if not open_trades:
            return
        try:
            positions = self.broker.positions()
        except Exception as e:
            log.warning("Pozisyonlar okunamadı: %s", e)
            return
        news_min = self.news.minutes_to_next(now)
        for t in open_trades:
            if t.symbol not in positions:
                continue  # closing - the monitor will finalize it
            try:
                P, ex = self._prepared(t.symbol)
                price = self._price(t.symbol) or float(ex["close"].iloc[-1])
                since = ex[ex.index >= from_iso(t.filled_at).floor("5min")]
                hi = max([price, *since["high"].tolist()])
                lo = min([price, *since["low"].tolist()])
                fav, adv = (hi, lo) if t.dir == 1 else (lo, hi)
                t.mfe_price = fav if t.mfe_price is None else (max(t.mfe_price, fav) if t.dir == 1 else min(t.mfe_price, fav))
                t.mae_price = adv if t.mae_price is None else (min(t.mae_price, adv) if t.dir == 1 else max(t.mae_price, adv))
                self._verify_protection(t)
                if not self.cfg.optimizer.enabled or t.status != "open":
                    continue
                ctx = build_context(P, t, now, price, news_min)
                for action in optimize(t, ctx, self.cfg.optimizer, self.cfg.risk.max_rr):
                    self._apply(t, action)
                    if t.status != "open":
                        break
                self.journal.upsert_trade(t)
            except Exception:
                log.exception("%s optimizasyon hatası", t.symbol)

    def _verify_protection(self, t: Trade) -> None:
        try:
            ids = self.broker.protective_ids(t.symbol)
        except Exception as e:
            log.warning("%s koruma emirleri okunamadı: %s", t.symbol, e)
            return
        if t.sl_order_id not in ids or t.tp_order_id not in ids:
            log.warning("%s SL/TP eksik, yeniden konuyor", t.symbol)
            if t.sl_order_id not in ids:
                t.sl_order_id = None
            if t.tp_order_id not in ids:
                t.tp_order_id = None
            self._protect(t, t.filled_qty or t.qty)
            if t.status == "open":
                self._persist(t, "PROTECTION_RESTORED", "Eksik SL/TP emri yeniden kondu")

    def _apply(self, t: Trade, a: Action) -> None:
        qty = t.filled_qty or t.qty
        if a.kind == "close":
            self.tg.send(M.early_exit(t, a.reason))
            self.journal.add_event(t.id, "OPTIMIZER_EXIT", a.reason, self._price(t.symbol))
            self._close_now(t, "Optimizasyon: " + a.reason)
        elif a.kind == "move_sl":
            old = t.sl
            new = self.broker.round_price(t.symbol, a.price)
            try:
                t.sl_order_id = self.broker.set_stop_loss(t.symbol, t.side, qty, new, t.sl_order_id)
            except StopWouldTrigger:
                self._close_now(t, "Optimizasyon: stop fiyatın gerisinde kaldı - " + a.reason)
                return
            t.sl = new
            if t.r_at(new) >= 0:
                t.be_done = True
            self._persist(t, "SL_MOVED", f"{fmt_price(old)} → {fmt_price(new)}: {a.reason}", new,
                          {"old": old, "new": new, "r_locked": t.r_at(new)})
            self.tg.send(M.sl_moved(t, old, a.reason))
        elif a.kind == "set_tp":
            old = t.tp
            new = self.broker.round_price(t.symbol, a.price)
            t.tp_order_id = self.broker.set_take_profit(t.symbol, t.side, qty, new, t.tp_order_id)
            t.tp = new
            t.tp_extended = True
            self._persist(t, "TP_EXTENDED", f"{fmt_price(old)} → {fmt_price(new)}: {a.reason}", new,
                          {"old": old, "new": new, "r": t.r_at(new)})
            self.tg.send(M.tp_moved(t, old, a.reason))

    # ------------------------------------------------------------------ telegram commands
    def handle_command(self, cmd: str, args: list[str]) -> str:
        with self.lock:
            if cmd in ("positions", "postions", "position", "pozisyonlar", "p"):
                return self.positions_text()
            if cmd in ("status", "durum"):
                return self.status_text()
            if cmd in ("stats", "rapor", "report", "kar", "pnl"):
                return self.stats_text()
            if cmd in ("news", "haber", "haberler"):
                return self.news_text()
            if cmd in ("pause", "dur"):
                self.kv.set_json("paused", True)
                return "⏸ Yeni işlem açma durduruldu. Açık pozisyonlar yönetilmeye devam ediyor."
            if cmd in ("resume", "devam"):
                self.kv.delete("paused")
                self.risk.resume()
                return "▶️ Yeni işlem açma aktif (risk molaları sıfırlandı)."
            if cmd in ("close", "kapat"):
                return self.close_command(args)
            return M.HELP

    def positions_text(self) -> str:
        now = now_utc()
        active = self._active()
        lines = [f"📊 <b>Pozisyonlar</b> ({len(self._active('open'))} açık / {len(self._active('pending'))} bekleyen, "
                 f"limit {self.cfg.risk.max_open_positions})"]
        for t in sorted(active, key=lambda x: x.status):
            lines.append(M.position_line(t, self._price(t.symbol) if t.status == "open" else None, now))
        try:
            managed = {t.symbol for t in active}
            for sym, p in self.broker.positions().items():
                if sym not in managed:
                    lines.append(f"ℹ️ Yönetilmeyen: {base_of(sym)} {p.side} x{p.qty:g} @ {fmt_price(p.entry_price)} "
                                 f"PnL {p.unrealized_pnl:+.2f}")
        except Exception:
            pass
        if not active:
            lines.append("Açık pozisyon yok.")
        return "\n".join(lines)

    def status_text(self) -> str:
        now = now_utc()
        rs = self.risk.s
        try:
            eq, free = self.broker.equity(), self.broker.free_balance()
        except Exception:
            eq = free = float("nan")
        blocked = self._block_reason(now)
        ls = self.last_scan
        return (
            f"🤖 <b>Durum</b> — {self.cfg.mode.upper()}\n"
            f"Bakiye: {fmt_usd(eq)} (serbest {fmt_usd(free)})\n"
            f"Bugün: {float(rs.get('realized_today', 0)):+.2f} USDT, {rs.get('trades_today', 0)} işlem\n"
            f"Açık risk: {sum(t.current_risk_usd() for t in self._active('open')):.2f} USDT\n"
            f"Yeni işlem: {'⛔ ' + blocked if blocked else '✅ açık'}\n"
            f"Son tarama: {M.local(from_iso(ls.get('time')), self.tz) if ls else '-'} "
            f"({ls.get('found', 0)} setup, {ls.get('placed', 0)} emir)\n"
            f"Sonraki tarama: {M.local(self.next_cycle, self.tz) if self.next_cycle is not None else '-'}\n"
            f"Coinler: {', '.join(base_of(s) for s in self.symbols)}\n"
            f"Depolama: {self.kv.backend} + {'postgres' if self.journal.enabled else 'csv'}"
        )

    def stats_text(self) -> str:
        """Performance of closed trades, straight from PostgreSQL."""
        if not self.journal.enabled:
            return "📈 İstatistik için PostgreSQL bağlantısı gerekli."
        mode = self.cfg.mode
        s = (self.journal.query(
            """SELECT count(*) AS n, count(*) FILTER (WHERE pnl > 0) AS wins,
                      coalesce(sum(pnl), 0) AS pnl, coalesce(avg(r_multiple), 0) AS avg_r,
                      coalesce(sum(pnl) FILTER (WHERE pnl > 0), 0) AS gross_win,
                      coalesce(-sum(pnl) FILTER (WHERE pnl < 0), 0) AS gross_loss,
                      coalesce(sum(fees), 0) AS fees,
                      coalesce(sum(pnl) FILTER (WHERE closed_at >= date_trunc('day', now())), 0) AS today,
                      coalesce(sum(pnl) FILTER (WHERE closed_at >= now() - interval '7 days'), 0) AS week,
                      coalesce(sum(pnl) FILTER (WHERE closed_at >= now() - interval '30 days'), 0) AS month,
                      min(created_at) AS since
               FROM trades WHERE mode = %s AND status = 'closed'""", (mode,)) or [{}])[0]
        start = self.journal.query("SELECT equity FROM equity_snapshots WHERE mode = %s ORDER BY ts LIMIT 1", (mode,))
        by_sym = self.journal.query(
            "SELECT symbol, count(*) AS n, sum(pnl) AS pnl FROM trades WHERE mode = %s AND status = 'closed' "
            "GROUP BY symbol ORDER BY pnl DESC", (mode,))
        setups = (self.journal.query(
            "SELECT count(*) AS found, count(*) FILTER (WHERE taken) AS taken FROM setups WHERE mode = %s",
            (mode,)) or [{}])[0]
        top_skip = self.journal.query(
            "SELECT split_part(reject_reason, ':', 1) AS reason, count(*) AS n FROM setups "
            "WHERE mode = %s AND NOT taken GROUP BY 1 ORDER BY 2 DESC LIMIT 1", (mode,))
        try:
            eq = self.broker.equity()
        except Exception:
            eq = None

        n = int(s.get("n") or 0)
        lines = [f"📈 <b>Performans</b> — {mode.upper()}"
                 + (f" ({s['since']:%d.%m.%Y}'den beri)" if s.get("since") else "")]
        if n == 0:
            lines.append("Henüz kapanan işlem yok.")
        else:
            wins = int(s["wins"])
            pf = s["gross_win"] / s["gross_loss"] if s["gross_loss"] else None
            lines += [
                f"Kapanan işlem: {n} | Kazanan: {wins} (%{wins / n * 100:.0f}) | Kaybeden: {n - wins}",
                f"Toplam PnL: <b>{s['pnl']:+.2f} USDT</b> | Ortalama {s['avg_r']:+.2f}R"
                + (f" | PF {pf:.2f}" if pf else ""),
                f"Ödenen komisyon: {s['fees']:.2f} USDT",
                f"Bugün {s['today']:+.2f} | 7 gün {s['week']:+.2f} | 30 gün {s['month']:+.2f}",
            ]
            if by_sym:
                best, worst = by_sym[0], by_sym[-1]
                lines.append(f"En iyi: {base_of(best['symbol'])} {best['pnl']:+.2f} ({best['n']}) | "
                             f"En kötü: {base_of(worst['symbol'])} {worst['pnl']:+.2f} ({worst['n']})")
        if eq is not None:
            line = f"Bakiye: {fmt_usd(eq)}"
            if start and start[0]["equity"]:
                first = float(start[0]["equity"])
                line += f" (başlangıç {fmt_usd(first)} → {(eq / first - 1) * 100:+.1f}%)"
            lines.append(line)
        if setups.get("found"):
            skip = f", en sık atlama: {top_skip[0]['reason']}" if top_skip else ""
            lines.append(f"Bulunan setup: {setups['found']}, alınan: {setups['taken']}{skip}")
        lines.append(f"Açık: {len(self._active('open'))} pozisyon, {len(self._active('pending'))} bekleyen emir")
        return "\n".join(lines)

    def news_text(self) -> str:
        now = now_utc()
        self.news.refresh()
        evs = self.news.upcoming(now, 72)
        if not self.news.has_data:
            return "⚠️ Haber verisi alınamadı."
        if not evs:
            return "📰 Önümüzdeki 72 saatte filtreye takılan önemli haber yok."
        lines = [f"📰 <b>Yaklaşan haberler</b> ({'/'.join(self.cfg.news.currencies)}, "
                 f"{'/'.join(self.cfg.news.impacts)}) — işlem öncesi {self.cfg.news.block_hours_before} saat blok"]
        for e in evs[:15]:
            lines.append(f"• {M.local(from_iso(e['time']), self.tz)} — {e['country']} {e['title']}")
        return "\n".join(lines)

    def close_command(self, args: list[str]) -> str:
        if not args:
            return "Kullanım: /close BTC"
        base = args[0].upper().replace("USDT", "")
        for t in self._active():
            if base_of(t.symbol) == base:
                if t.status == "pending":
                    self.broker.cancel_order(t.symbol, t.entry_order_id)
                    self._finish_cancel(t, "Kullanıcı iptal etti (/close)")
                    return f"✅ {base} bekleyen emri iptal edildi."
                self._close_now(t, "Kullanıcı kapattı (/close)")
                return f"✅ {base} pozisyonu kapatıldı."
        return f"{base} için bot tarafından yönetilen pozisyon yok."
