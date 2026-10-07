"""Live engine.

Every ``scan_interval_minutes`` (5), right after the candle closes:
    1. housekeeping (equity, risk breakers, news calendar, expired pending orders)
    2. SCAN      - SMC setups (every cycle) and trend breakouts (when a new 4H candle closed)
    3. OPTIMIZE  - position management of every open position (SMC optimizer / trend trailing stop)
Between cycles a fast monitor loop (15 s) detects fills and closes so that a new position gets
its stop-loss within seconds.

Each strategy trades in an *account*: ``main`` (paper/demo/live, per ``mode``) or ``shadow``
(an always-virtual paper account used to keep observing a strategy without risking money).
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import ccxt
import pandas as pd

from . import messages as M
from . import trend as T
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


def public_exchange() -> ccxt.Exchange:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    ex.load_markets()
    return ex


def make_broker(cfg, kv) -> Broker:
    if cfg.mode == "paper":
        return PaperBroker(MarketData(public_exchange()), kv, cfg.paper.start_balance, cfg.risk.fees)
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


@dataclass
class Account:
    name: str       # main | shadow
    broker: Broker
    mode: str       # label stored in PostgreSQL: paper/demo/live or shadow


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tz = cfg.telegram.get("display_timezone", "UTC")
        self.kv = make_kv(cfg.secrets.redis_url, cfg.storage.redis_prefix, cfg.storage.state_file)
        self.journal = Journal(cfg.secrets.database_url, cfg.mode)
        if self.journal.enabled:
            logging.getLogger().addHandler(DBLogHandler(self.journal, cfg.storage.db_log_level))
        # enabled strategies, those trading the main account first (display order)
        self.strategies = dict(sorted(((n, sc) for n, sc in cfg.strategies.items() if sc.get("enabled", True)),
                                      key=lambda x: x[1].account != "main"))
        self.accounts: dict[str, Account] = {"main": Account("main", make_broker(cfg, self.kv), cfg.mode)}
        if any(sc.account == "shadow" for sc in self.strategies.values()):
            shadow = PaperBroker(MarketData(public_exchange()), self.kv, cfg.paper.start_balance, cfg.risk.fees,
                                 key="paper:shadow")
            self.accounts["shadow"] = Account("shadow", shadow, "shadow")
        self.broker = self.accounts["main"].broker
        self.md = MarketData(self.broker.exchange)
        self.symbols = resolve_universe(self.broker.exchange, cfg)
        self.news = NewsCalendar(cfg.news, self.kv)
        self.tg = Telegram(cfg.secrets.telegram_token, cfg.secrets.telegram_chat_id, cfg.telegram.enabled, self.kv)
        # one set of circuit breakers per strategy (trend tolerates losing streaks, SMC does not)
        self.guards = {"smc": RiskGuard(cfg.risk, self.kv, key="risk:smc")}
        if "trend" in cfg.strategies:
            self.guards["trend"] = RiskGuard(cfg.strategies.trend.guard, self.kv, key="risk:trend")
        self.lock = threading.RLock()
        self.trades: dict[str, Trade] = {k: Trade.from_dict(v) for k, v in (self.kv.get_json("trades") or {}).items()}
        self._stop = threading.Event()
        self._cache: dict[str, dict] = {}
        self._trend_cache: dict[str, T.TrendState] = {}
        self._last_snapshot = 0.0
        self.next_cycle: pd.Timestamp | None = None
        self.last_scan: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    def _acct(self, t: Trade) -> Account:
        return self.accounts.get(t.account) or self.accounts["main"]

    def _b(self, t: Trade) -> Broker:
        return self._acct(t).broker

    def _guard(self, strategy: str) -> RiskGuard:
        return self.guards.get(strategy, self.guards["smc"])

    def _active(self, status: str | None = None, strategy: str | None = None, account: str | None = None) -> list[Trade]:
        return [t for t in self.trades.values()
                if t.status in (("pending", "open") if status is None else (status,))
                and (strategy is None or t.strategy == strategy) and (account is None or t.account == account)]

    def _notify(self, t: Trade, text: str) -> None:
        if t.account == "shadow" and not self.cfg.strategies.get(t.strategy, {}).get("notify", True):
            return
        self.tg.send(M.tag(t) + "\n" + text)

    # ------------------------------------------------------------------ persistence
    def _save(self) -> None:
        self.trades = {k: t for k, t in self.trades.items() if t.status in ("pending", "open")}
        self.kv.set_json("trades", {k: t.to_dict() for k, t in self.trades.items()})

    def _persist(self, t: Trade, event: str | None = None, message: str = "", price: float | None = None,
                 data: dict | None = None) -> None:
        if event:
            t.add_log(event, message)
            self.journal.add_event(t.id, event, message, price, data)
        self.journal.upsert_trade(t, mode=self._acct(t).mode)
        self._save()

    # ------------------------------------------------------------------ main loop
    def run(self, once: bool = False) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except ValueError:
                pass
        self.news.refresh()  # the Redis cache survives restarts; the feed rate-limits repeated downloads
        lines = []
        for name, sc in self.strategies.items():
            acct = self.accounts[sc.account]
            lines.append(f"• {name.upper()}: {'ana hesap (' + self.cfg.mode.upper() + ')' if acct.name == 'main' else 'gözlem hesabı (sanal)'}"
                         f" — bakiye {fmt_usd(acct.broker.equity())}")
        log.info("Bot başladı | mod=%s | stratejiler=%s | coin=%d | state=%s | db=%s", self.cfg.mode,
                 ",".join(self.strategies), len(self.symbols), self.kv.backend,
                 "postgres" if self.journal.enabled else "csv")
        self.tg.send(f"🤖 <b>Bot başladı</b> ({self.cfg.mode.upper()})\n" + "\n".join(lines)
                     + f"\nCoin: {len(self.symbols)} | Aktif işlem: {len(self._active())}\n/help ile komutlar")
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
        self.tg.send("⏹ Bot durduruldu. Açık pozisyonların stop emirleri borsada duruyor.")
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
            managed = {t.symbol for t in self._active(account="main")}
            other = [p for s, p in self.broker.positions().items() if s not in managed]
            if other:
                self.tg.send("ℹ️ Bot tarafından yönetilmeyen pozisyonlar var (dokunulmayacak): "
                             + ", ".join(f"{base_of(p.symbol)} {p.side}" for p in other))
        except Exception as e:
            log.warning("Pozisyon kontrolü yapılamadı: %s", e)

    # ------------------------------------------------------------------ fast monitor
    def _positions(self, account_names) -> dict[str, dict]:
        return {name: self.accounts[name].broker.positions() for name in account_names}

    def monitor(self) -> None:
        for acct in self.accounts.values():
            acct.broker.sync()
        active = self._active()
        if not active:
            return
        positions = self._positions({t.account for t in active if t.account in self.accounts})
        for t in active:
            try:
                pos = positions.get(t.account, {})
                if t.status == "pending":
                    self._check_pending(t, pos)
                elif t.status == "open":
                    self._check_open(t, pos)
            except Exception:
                log.exception("%s kontrol hatası", t.symbol)

    def _check_pending(self, t: Trade, positions) -> None:
        b = self._b(t)
        st = b.order_state(t.symbol, t.entry_order_id)
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
                self._notify(t, M.filled(t))
        elif st.status == "canceled":
            self._finish_cancel(t, "Emir borsada iptal edildi")

    def _check_open(self, t: Trade, positions) -> None:
        b = self._b(t)
        pos = positions.get(t.symbol)
        if t.entry_open:
            st = b.order_state(t.symbol, t.entry_order_id)
            if st.status != "open":
                t.entry_open = False
        if pos is None or pos.qty <= 0 or pos.side != t.side:
            if t.entry_open:
                b.cancel_order(t.symbol, t.entry_order_id)
                t.entry_open = False
            self._finalize(t)
            return
        if abs(pos.qty - t.protected_qty) > 1e-12:
            t.filled_qty = pos.qty
            t.fill_price = pos.entry_price or t.fill_price
            self._protect(t, pos.qty)
            self._persist(t, "QTY_CHANGED", f"Pozisyon miktarı {pos.qty:g}, SL/TP güncellendi")

    def _protect(self, t: Trade, qty: float) -> None:
        """Place / resize SL (and TP when the strategy has one). No stop -> close immediately."""
        b = self._b(t)
        try:
            t.sl_order_id = b.set_stop_loss(t.symbol, t.side, qty, t.sl, t.sl_order_id)
        except StopWouldTrigger:
            self._close_now(t, "Stop seviyesi fiyatın gerisinde kaldı (koruma)")
            return
        except Exception as e:
            log.exception("%s SL konulamadı", t.symbol)
            self._notify(t, f"🚨 {base_of(t.symbol)} STOP KONULAMADI ({e}). Pozisyon güvenlik için kapatılıyor.")
            self._close_now(t, "SL konulamadı (güvenlik kapanışı)")
            return
        if t.tp is not None:
            try:
                t.tp_order_id = b.set_take_profit(t.symbol, t.side, qty, t.tp, t.tp_order_id)
            except Exception as e:
                log.warning("%s TP konulamadı: %s (SL aktif, tekrar denenecek)", t.symbol, e)
        t.protected_qty = qty

    def _close_now(self, t: Trade, reason: str) -> None:
        qty = t.filled_qty or t.qty
        try:
            self._b(t).close_position(t.symbol, t.side, qty)
        except Exception as e:
            log.exception("%s kapatılamadı", t.symbol)
            self._notify(t, f"🚨 {base_of(t.symbol)} pozisyonu KAPATILAMADI: {e}. Lütfen manuel kontrol edin!")
            return
        time.sleep(1.0)
        self._finalize(t, reason)

    @staticmethod
    def _exit_reason(t: Trade, exit_px: float) -> str:
        if t.tp is not None and abs(exit_px - t.tp) < abs(exit_px - t.sl):
            return "TP" + (" (uzatılmış)" if t.tp_extended else "")
        if t.r_at(t.sl) < -0.2:
            return "SL"
        if t.strategy == "trend":
            return "Trailing stop"
        return "Break-even stop" if t.r_at(t.sl) <= 0.2 else "Kâr kilidi / trailing stop"

    def _finalize(self, t: Trade, reason: str | None = None) -> None:
        now = now_utc()
        b = self._b(t)
        since_ms = int(ts_ns(from_iso(t.created_at)) // 1_000_000)
        try:
            res = b.close_result(t.symbol, since_ms)
        except Exception as e:
            log.warning("%s kapanış sonucu okunamadı: %s", t.symbol, e)
            res = None
        b.cancel_all(t.symbol)
        exit_px = (res.exit_price if res else None) or self._price(t.symbol) or t.sl
        t.status = "closed"
        t.closed_at = to_iso(now)
        t.exit_price = float(exit_px)
        t.exit_reason = reason or self._exit_reason(t, exit_px)
        t.r_multiple = round(t.r_at(exit_px), 3)
        if res is not None:
            t.pnl, t.fees = round(res.pnl, 4), round(res.fees, 4)
        else:
            t.pnl = t.dir * (exit_px - t.open_price) * (t.filled_qty or t.qty)
        breaker = self._guard(t.strategy).register_close(t.pnl or 0.0, now)
        cooldown = self.cfg.risk.symbol_cooldown_minutes if t.strategy == "smc" else 0
        if cooldown:
            self.kv.set_json(f"cooldown:{t.account}:{t.symbol}", 1, ttl=int(cooldown * 60))
        self._persist(t, "CLOSED", f"{t.exit_reason} @ {fmt_price(exit_px)} | PnL {t.pnl:+.2f} ({t.r_multiple:+.2f}R)",
                      exit_px)
        log.info("[%s/%s] %s %s kapandı: %s PnL=%.2f R=%.2f", t.strategy, t.account, t.symbol, t.side,
                 t.exit_reason, t.pnl or 0, t.r_multiple)
        self._notify(t, M.closed(t))
        if breaker:
            self._notify(t, "⛔ " + breaker)

    def _finish_cancel(self, t: Trade, reason: str) -> None:
        t.status = "cancelled"
        t.exit_reason = reason
        t.closed_at = to_iso(now_utc())
        self._persist(t, "CANCELLED", reason)
        self._notify(t, M.cancelled(t, reason))

    # ------------------------------------------------------------------ 5-minute cycle
    def cycle(self) -> None:
        t0 = time.time()
        now = now_utc()
        self._cache = {}
        self._trend_cache = {}
        equities = {name: acct.broker.equity() for name, acct in self.accounts.items()}
        for name, sc in self.strategies.items():
            self._guard(name).on_cycle(equities[sc.account], now)
        self.news.refresh()
        self._manage_pending(now)
        if "trend" in self.strategies:
            self.scan_trend(now)        # 1a) trend entries (on new 4H candles) - fast, main account first
        if "smc" in self.strategies:
            self.scan_smc(now)          # 1b) SMC entries (30 coins x 4 timeframes)
        self.optimize_positions(now)    # 2) position management
        if time.time() - self._last_snapshot >= self.cfg.storage.equity_snapshot_minutes * 60:
            self._last_snapshot = time.time()
            for name, acct in self.accounts.items():
                opened = self._active("open", account=name)
                self.journal.snapshot_equity(equities[name], acct.broker.free_balance(), len(opened),
                                             sum(t.current_risk_usd() for t in opened), mode=acct.mode)
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

    def _trend_state(self, symbol: str) -> T.TrendState:
        if symbol not in self._trend_cache:
            h4 = self.md.candles(symbol, "4h", 300)
            d1 = self.md.candles(symbol, "1d", 250)
            self._trend_cache[symbol] = T.analyze(h4, d1, self.cfg.strategies.trend)
        return self._trend_cache[symbol]

    def _price(self, symbol: str) -> float | None:
        try:
            return self.md.last_price(symbol)
        except Exception:
            return None

    def _manage_pending(self, now: pd.Timestamp) -> None:
        for t in self._active("pending"):
            b = self._b(t)
            price = self._price(t.symbol)
            reason = None
            if now >= from_iso(t.expires_at):
                reason = "Süre doldu, fiyat giriş bölgesine gelmedi"
            elif price is not None and t.cancel_price is not None and t.dir * (price - t.cancel_price) >= 0:
                reason = "Fiyat dolmadan hedefe gitti"
            if reason:
                b.cancel_order(t.symbol, t.entry_order_id)
                self._check_pending(t, b.positions())  # a last-second fill turns it into a position
                if t.status == "pending":
                    self._finish_cancel(t, reason)
        for t in self._active("open"):
            if t.entry_open and now >= from_iso(t.expires_at):
                self._b(t).cancel_order(t.symbol, t.entry_order_id)
                t.entry_open = False
                self._persist(t, "ENTRY_REMAINDER_CANCELLED", "Kısmi dolum: kalan emir iptal edildi")

    # ------------------------------------------------------------------ scanning
    def _block_reason(self, now: pd.Timestamp, strategy: str, news: bool) -> str | None:
        ok, reason = self._guard(strategy).can_open(now)
        if not ok:
            return reason
        if self.kv.get_json("paused"):
            return "Kullanıcı durdurdu (/pause)"
        if news:
            ev = self.news.blocking_event(now)
            if ev:
                if ev.get("country") == "-":
                    return ev["title"]
                return f"Haber filtresi: {ev['title']} ({ev['country']}) {M.local(from_iso(ev['time']), self.tz)}"
        return None

    def _occupied(self, account: str) -> set[str]:
        occ = {t.symbol for t in self._active(account=account)}
        try:
            occ |= set(self.accounts[account].broker.positions())
        except Exception as e:
            log.warning("Pozisyonlar okunamadı (%s): %s", account, e)
        return occ

    def scan_smc(self, now: pd.Timestamp) -> None:
        t0 = time.time()
        sc = self.cfg.strategies.smc
        acct = self.accounts[sc.account]
        blocked = self._block_reason(now, "smc", news=True)
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

        occupied = self._occupied(acct.name)
        slots = self.cfg.risk.max_open_positions - len(self._active(strategy="smc"))
        placed = 0
        for s in candidates:
            final = False
            if blocked:
                reject = blocked
            elif s.symbol in occupied:
                reject = "Bu coinde zaten pozisyon/emir var"
            elif self.kv.exists(f"cooldown:{acct.name}:{s.symbol}"):
                reject = "Coin bekleme süresinde (son işlem yeni kapandı)"
            elif slots <= 0:
                reject = f"Maksimum pozisyon sayısı ({self.cfg.risk.max_open_positions}) dolu"
            else:
                reject, final = self._validate_smc(s, now)
                if reject is None:
                    risk_pct = risk_pct_for_score(s.score, self.cfg.risk)
                    reject, final = self._open(s, acct, risk_pct, slots, self.cfg.risk.max_total_risk_pct,
                                               "market" if self.cfg.strategy.entry_mode == "market" else "limit")
                    if reject is None:
                        placed += 1
                        slots -= 1
                        occupied.add(s.symbol)
            if reject is None or final:
                self.kv.set_json(f"setup:{s.id}", reject or "taken", ttl=SETUP_TTL)
            self.journal.record_setup(s, reject is None, reject, mode=acct.mode)
            if reject:
                log.info("SMC setup atlandı %s skor=%d: %s", s.id, s.score, reject)

        self.last_scan["smc"] = {"time": to_iso(now), "found": len(candidates), "placed": placed, "blocked": blocked}
        self.journal.record_scan(int((time.time() - t0) * 1000), len(prepared), len(candidates), placed, blocked,
                                 {"strategy": "smc", "candidates": [{"id": s.id, "score": s.score} for s in candidates[:20]]},
                                 mode=acct.mode)
        log.info("SMC tarama: %d coin, %d yeni setup, %d emir%s", len(prepared), len(candidates), placed,
                 f" | engel: {blocked}" if blocked else "")

    def scan_trend(self, now: pd.Timestamp) -> None:
        """Runs once per closed 4H candle: breakout signals on that candle."""
        tc = self.cfg.strategies.trend
        bar_close = now.floor("4h")
        key = "trend:last_bar"
        if self.kv.get_json(key) == to_iso(bar_close):
            return
        if now - bar_close > pd.Timedelta(minutes=tc.max_signal_age_minutes):
            self.kv.set_json(key, to_iso(bar_close))  # started too late for this candle
            return
        t0 = time.time()
        acct = self.accounts[tc.account]
        blocked = self._block_reason(now, "trend", news=tc.news_filter)
        candidates: list[Setup] = []
        ok = 0
        for sym in self.symbols:
            try:
                st = self._trend_state(sym)
            except Exception as e:
                log.warning("%s 4H veri alınamadı: %s", sym, e)
                continue
            ok += 1
            i = len(st.close_ns) - 1
            if i < 0 or st.close_ns[i] != ts_ns(bar_close):
                continue  # the candle that just closed is not in the data yet
            s = T.signal_at(sym, st, i, tc)
            if s is not None:
                candidates.append(s)
        if ok == 0:
            return  # retry on the next cycle
        occupied = self._occupied(acct.name)
        slots = tc.max_open_positions - len(self._active(strategy="trend"))
        placed = 0
        for s in candidates:
            reject, final = None, True
            if blocked:
                reject = blocked
            elif s.symbol in occupied:
                reject = "Bu coinde zaten pozisyon var"
            elif slots <= 0:
                reject = f"Maksimum trend pozisyonu ({tc.max_open_positions}) dolu"
            else:
                price = self._price(s.symbol)
                if price is None:
                    reject = "Fiyat alınamadı"
                elif s.dir * (price - s.sl) <= 0:
                    reject = "Fiyat stop seviyesinin gerisinde"
                elif abs(price - s.entry) > tc.max_entry_drift_r * s.risk:
                    reject = f"Fiyat kaçtı (> {tc.max_entry_drift_r}R)"
                elif (weight := T.risk_multiplier(s, tc))[0] <= 0:
                    reject = f"Sinyal kuralı: {weight[1]}"
                else:
                    s.entry = price  # market order at the current price; the stop stays at the ATR level
                    gs = self._guard("trend").s
                    risk_pct = (tc.risk_pct_long if s.side == "long" else tc.risk_pct_short) * weight[0]
                    risk_pct *= T.drawdown_multiplier(gs.get("peak_equity"), gs.get("last_equity") or 0, tc)
                    if weight[1]:
                        s.reasons.append(f"Ağırlık: {weight[1]}")
                    reject, final = self._open(s, acct, risk_pct, slots, tc.max_total_risk_pct, "market")
                    if reject is None:
                        placed += 1
                        slots -= 1
                        occupied.add(s.symbol)
            self.journal.record_setup(s, reject is None, reject, mode=acct.mode)
            if reject:
                log.info("Trend sinyali atlandı %s: %s", s.id, reject)
        self.kv.set_json(key, to_iso(bar_close))
        self.last_scan["trend"] = {"time": to_iso(now), "found": len(candidates), "placed": placed, "blocked": blocked}
        self.journal.record_scan(int((time.time() - t0) * 1000), ok, len(candidates), placed, blocked,
                                 {"strategy": "trend", "bar": to_iso(bar_close),
                                  "candidates": [s.id for s in candidates]}, mode=acct.mode)
        log.info("Trend tarama (4H %s): %d coin, %d sinyal, %d emir%s", bar_close.strftime("%m-%d %H:%M"), ok,
                 len(candidates), placed, f" | engel: {blocked}" if blocked else "")

    def _validate_smc(self, s: Setup, now: pd.Timestamp) -> tuple[str | None, bool]:
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

    def _open(self, s: Setup, acct: Account, risk_pct: float, slots: int, max_total_risk_pct: float,
              order_type: str) -> tuple[str | None, bool]:
        rc = self.cfg.risk
        b = acct.broker
        if risk_pct <= 0:
            return "Skor risk basamaklarının altında", True
        try:
            equity, free = b.equity(), b.free_balance()
        except Exception as e:
            return f"Hesap bilgisi alınamadı: {e}", False
        same = self._active(account=acct.name)
        open_risk = sum(t.current_risk_usd() for t in same if t.status == "open") + \
            sum(t.risk_usd for t in same if t.status == "pending")
        room = equity * max_total_risk_pct / 100 - open_risk
        if room < equity * risk_pct / 100:
            if room < equity * 0.002:
                return f"Toplam açık risk limiti (%{max_total_risk_pct}) dolu", False
            risk_pct = room / equity * 100
        qty, _ = position_size(equity, s.entry, s.sl, risk_pct, rc.fees)
        plan = choose_leverage(qty * s.entry, free, slots, abs(s.entry - s.sl) / s.entry, rc.leverage_max)
        qty = b.round_qty(s.symbol, qty * plan.qty_scale)
        rules = b.rules(s.symbol)
        if qty <= 0 or qty < rules.min_qty or qty * s.entry < rules.min_notional:
            return f"Pozisyon borsa minimumunun altında (min {rules.min_notional:g} USDT)", True
        risk_usd = qty * (abs(s.entry - s.sl) + s.entry * (rc.fees.maker + rc.fees.taker))
        entry = b.round_price(s.symbol, s.entry)
        sl = b.round_price(s.symbol, s.sl)
        tp = b.round_price(s.symbol, s.tp) if s.tp is not None else None
        tag = "T" if s.strategy == "trend" else ""
        tid = f"{base_of(s.symbol)}-{s.created_at:%Y%m%d%H%M}-{tag}{s.side[0].upper()}" + \
            ("-S" if acct.name == "shadow" else "")
        try:
            b.prepare(s.symbol, plan.leverage)
            oid = b.place_entry(s.symbol, s.side, qty, entry, (s.strategy[:3] + tid.replace("-", ""))[:36], order_type)
        except Exception as e:
            log.exception("%s emir gönderilemedi", s.symbol)
            self.tg.send(f"⚠️ {base_of(s.symbol)} emir gönderilemedi: {e}")
            return f"Emir hatası: {e}", False
        t = Trade(
            id=tid, symbol=s.symbol, side=s.side, status="pending", setup_id=s.id, score=s.score,
            reasons=s.reasons, poi=s.poi, swept=s.swept, entry=entry, sl=sl, tp=tp, initial_sl=sl,
            initial_tp=tp, tp_r=s.tp_r, qty=qty, risk_pct=round(risk_usd / equity * 100, 3),
            risk_usd=round(risk_usd, 4), leverage=plan.leverage, created_at=to_iso(now_utc()),
            expires_at=to_iso(s.expires_at), cancel_price=tp if order_type == "limit" else None,
            targets=s.targets, setup=s.to_dict(), entry_order_id=oid, strategy=s.strategy, account=acct.name,
        )
        self.trades[t.id] = t
        kind = "market" if order_type == "market" else "limit"
        self._persist(t, "ORDER_PLACED", f"{kind} emir @ {fmt_price(entry)}", entry, s.to_dict())
        self._notify(t, M.order_placed(t, self.tz, kind))
        log.info("[%s/%s] Emir: %s %s @ %s SL %s TP %s qty %s risk %.2f%%", s.strategy, acct.name, s.symbol, s.side,
                 entry, sl, tp, qty, t.risk_pct)
        try:  # protect a market fill right away instead of waiting for the monitor loop
            b.sync()
            self._check_pending(t, b.positions())
        except Exception:
            log.exception("%s ilk koruma kontrolü", s.symbol)
        return None, True

    # ------------------------------------------------------------------ position management
    def optimize_positions(self, now: pd.Timestamp) -> None:
        open_trades = self._active("open")
        if not open_trades:
            return
        try:
            positions = self._positions({t.account for t in open_trades if t.account in self.accounts})
        except Exception as e:
            log.warning("Pozisyonlar okunamadı: %s", e)
            return
        news_min = self.news.minutes_to_next(now)
        for t in open_trades:
            if t.symbol not in positions.get(t.account, {}):
                continue  # closing - the monitor will finalize it
            try:
                if t.strategy == "trend":
                    self._manage_trend(t, now)
                else:
                    self._manage_smc(t, now, news_min)
                if t.status == "open":
                    self.journal.upsert_trade(t, mode=self._acct(t).mode)
            except Exception:
                log.exception("%s pozisyon yönetimi hatası", t.symbol)

    def _track_excursion(self, t: Trade, highs, lows, price: float) -> None:
        hi = max([price, *highs])
        lo = min([price, *lows])
        fav, adv = (hi, lo) if t.dir == 1 else (lo, hi)
        t.mfe_price = fav if t.mfe_price is None else (max(t.mfe_price, fav) if t.dir == 1 else min(t.mfe_price, fav))
        t.mae_price = adv if t.mae_price is None else (min(t.mae_price, adv) if t.dir == 1 else max(t.mae_price, adv))

    def _manage_smc(self, t: Trade, now: pd.Timestamp, news_min: float | None) -> None:
        P, ex = self._prepared(t.symbol)
        price = self._price(t.symbol) or float(ex["close"].iloc[-1])
        since = ex[ex.index >= from_iso(t.filled_at).floor("5min")]
        self._track_excursion(t, since["high"].tolist(), since["low"].tolist(), price)
        self._verify_protection(t)
        if not self.cfg.optimizer.enabled or t.status != "open":
            return
        ctx = build_context(P, t, now, price, news_min)
        for action in optimize(t, ctx, self.cfg.optimizer, self.cfg.risk.max_rr):
            self._apply(t, action)
            if t.status != "open":
                break

    def _manage_trend(self, t: Trade, now: pd.Timestamp) -> None:
        tc = self.cfg.strategies.trend
        st = self._trend_state(t.symbol)
        price = self._price(t.symbol) or float(st.h4["close"].iloc[-1])
        entry_bar = from_iso(t.setup.get("created_at")) if t.setup.get("created_at") else from_iso(t.filled_at)
        h4 = st.h4[st.h4.index >= entry_bar]  # candles that opened after the entry
        self._track_excursion(t, h4["high"].tolist(), h4["low"].tolist(), price)
        self._verify_protection(t)
        if t.status != "open":
            return
        cand = T.trail_candidate(st, ts_ns(entry_bar), t.dir, tc)
        if cand is None:
            return
        if t.dir * (price - cand) <= 0:
            self._apply(t, Action("close", None, f"Fiyat iz süren stopun ({fmt_price(cand)}) gerisine geçti"))
        elif t.dir * (cand - t.sl) >= tc.min_stop_step_r * t.risk_unit:
            self._apply(t, Action("move_sl", cand, f"İz süren stop ({tc.atr_mult:g}×ATR 4H)"))

    def _verify_protection(self, t: Trade) -> None:
        try:
            ids = self._b(t).protective_ids(t.symbol)
        except Exception as e:
            log.warning("%s koruma emirleri okunamadı: %s", t.symbol, e)
            return
        tp_missing = t.tp is not None and t.tp_order_id not in ids
        if t.sl_order_id not in ids or tp_missing:
            log.warning("%s SL/TP eksik, yeniden konuyor", t.symbol)
            if t.sl_order_id not in ids:
                t.sl_order_id = None
            if tp_missing:
                t.tp_order_id = None
            self._protect(t, t.filled_qty or t.qty)
            if t.status == "open":
                self._persist(t, "PROTECTION_RESTORED", "Eksik SL/TP emri yeniden kondu")

    def _apply(self, t: Trade, a: Action) -> None:
        b = self._b(t)
        qty = t.filled_qty or t.qty
        if a.kind == "close":
            self._notify(t, M.early_exit(t, a.reason))
            self.journal.add_event(t.id, "OPTIMIZER_EXIT", a.reason, self._price(t.symbol))
            self._close_now(t, ("Trailing stop: " if t.strategy == "trend" else "Optimizasyon: ") + a.reason)
        elif a.kind == "move_sl":
            old = t.sl
            new = b.round_price(t.symbol, a.price)
            try:
                t.sl_order_id = b.set_stop_loss(t.symbol, t.side, qty, new, t.sl_order_id)
            except StopWouldTrigger:
                self._close_now(t, "Stop fiyatın gerisinde kaldı - " + a.reason)
                return
            t.sl = new
            if t.r_at(new) >= 0:
                t.be_done = True
            self._persist(t, "SL_MOVED", f"{fmt_price(old)} → {fmt_price(new)}: {a.reason}", new,
                          {"old": old, "new": new, "r_locked": t.r_at(new)})
            self._notify(t, M.sl_moved(t, old, a.reason))
        elif a.kind == "set_tp":
            old = t.tp
            new = b.round_price(t.symbol, a.price)
            t.tp_order_id = b.set_take_profit(t.symbol, t.side, qty, new, t.tp_order_id)
            t.tp = new
            t.tp_extended = True
            self._persist(t, "TP_EXTENDED", f"{fmt_price(old)} → {fmt_price(new)}: {a.reason}", new,
                          {"old": old, "new": new, "r": t.r_at(new)})
            self._notify(t, M.tp_moved(t, old, a.reason))

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
                for g in self.guards.values():
                    g.resume()
                return "▶️ Yeni işlem açma aktif (risk molaları sıfırlandı)."
            if cmd in ("close", "kapat"):
                return self.close_command(args)
            return M.HELP

    def positions_text(self) -> str:
        now = now_utc()
        lines = ["📊 <b>Pozisyonlar</b>"]
        for name, sc in self.strategies.items():
            trades = self._active(strategy=name)
            limit = sc.get("max_open_positions") or self.cfg.risk.max_open_positions
            title = M.strategy_title(name, sc.account)
            lines.append(f"\n<b>{title}</b> — {len([t for t in trades if t.status == 'open'])} açık, limit {limit}")
            for t in sorted(trades, key=lambda x: x.status):
                lines.append(M.position_line(t, self._price(t.symbol) if t.status == "open" else None, now))
            if not trades:
                lines.append("Açık pozisyon yok.")
        try:
            managed = {t.symbol for t in self._active(account="main")}
            for sym, p in self.broker.positions().items():
                if sym not in managed:
                    lines.append(f"ℹ️ Yönetilmeyen: {base_of(sym)} {p.side} x{p.qty:g} @ {fmt_price(p.entry_price)} "
                                 f"PnL {p.unrealized_pnl:+.2f}")
        except Exception:
            pass
        return "\n".join(lines)

    def status_text(self) -> str:
        now = now_utc()
        lines = [f"🤖 <b>Durum</b> — {self.cfg.mode.upper()}"]
        for name, sc in self.strategies.items():
            acct = self.accounts[sc.account]
            rs = self._guard(name).s
            try:
                eq, free = acct.broker.equity(), acct.broker.free_balance()
            except Exception:
                eq = free = float("nan")
            blocked = self._block_reason(now, name, news=(name == "smc" or sc.get("news_filter", False)))
            ls = self.last_scan.get(name, {})
            opened = self._active("open", strategy=name)
            lines += [
                f"\n<b>{M.strategy_title(name, sc.account)}</b>",
                f"Bakiye: {fmt_usd(eq)} (serbest {fmt_usd(free)})",
                f"Bugün: {float(rs.get('realized_today', 0)):+.2f} USDT, {rs.get('trades_today', 0)} işlem",
                f"Açık: {len(opened)} pozisyon, risk {sum(t.current_risk_usd() for t in opened):.2f} USDT",
                f"Yeni işlem: {'⛔ ' + blocked if blocked else '✅ açık'}",
                f"Son tarama: {M.local(from_iso(ls.get('time')), self.tz) if ls else '-'} "
                f"({ls.get('found', 0)} sinyal, {ls.get('placed', 0)} emir)",
            ]
        lines += [
            f"\nSonraki tarama: {M.local(self.next_cycle, self.tz) if self.next_cycle is not None else '-'}",
            f"Coin: {len(self.symbols)} | Depolama: {self.kv.backend} + {'postgres' if self.journal.enabled else 'csv'}",
        ]
        return "\n".join(lines)

    def stats_text(self) -> str:
        """Performance of closed trades per strategy, straight from PostgreSQL."""
        if not self.journal.enabled:
            return "📈 İstatistik için PostgreSQL bağlantısı gerekli."
        out = ["📈 <b>Performans</b>"]
        for name, sc in self.strategies.items():
            acct = self.accounts[sc.account]
            out.append(f"\n<b>{M.strategy_title(name, sc.account)}</b>")
            out += self._stats_lines(acct, name)
        return "\n".join(out)

    def _stats_lines(self, acct: Account, strategy: str) -> list[str]:
        mode = acct.mode
        where = "mode = %s AND coalesce(strategy, 'smc') = %s"
        s = (self.journal.query(
            f"""SELECT count(*) AS n, count(*) FILTER (WHERE pnl > 0) AS wins,
                      coalesce(sum(pnl), 0) AS pnl, coalesce(avg(r_multiple), 0) AS avg_r,
                      coalesce(sum(pnl) FILTER (WHERE pnl > 0), 0) AS gross_win,
                      coalesce(-sum(pnl) FILTER (WHERE pnl < 0), 0) AS gross_loss,
                      coalesce(sum(fees), 0) AS fees,
                      coalesce(sum(pnl) FILTER (WHERE closed_at >= date_trunc('day', now())), 0) AS today,
                      coalesce(sum(pnl) FILTER (WHERE closed_at >= now() - interval '7 days'), 0) AS week,
                      coalesce(sum(pnl) FILTER (WHERE closed_at >= now() - interval '30 days'), 0) AS month,
                      min(created_at) AS since
               FROM trades WHERE {where} AND status = 'closed'""", (mode, strategy)) or [{}])[0]
        start = self.journal.query("SELECT equity FROM equity_snapshots WHERE mode = %s ORDER BY ts LIMIT 1", (mode,))
        by_sym = self.journal.query(
            f"SELECT symbol, count(*) AS n, sum(pnl) AS pnl FROM trades WHERE {where} AND status = 'closed' "
            "GROUP BY symbol ORDER BY pnl DESC", (mode, strategy))
        setups = (self.journal.query(
            f"SELECT count(*) AS found, count(*) FILTER (WHERE taken) AS taken FROM setups WHERE {where}",
            (mode, strategy)) or [{}])[0]
        top_skip = self.journal.query(
            f"SELECT split_part(reject_reason, ':', 1) AS reason, count(*) AS n FROM setups "
            f"WHERE {where} AND NOT taken GROUP BY 1 ORDER BY 2 DESC LIMIT 1", (mode, strategy))
        try:
            eq = acct.broker.equity()
        except Exception:
            eq = None
        lines = []
        n = int(s.get("n") or 0)
        if n == 0:
            lines.append("Henüz kapanan işlem yok.")
        else:
            wins = int(s["wins"])
            pf = s["gross_win"] / s["gross_loss"] if s["gross_loss"] else None
            lines += [
                f"Başlangıç {s['since']:%d.%m.%Y} | {n} işlem | Kazanan {wins} (%{wins / n * 100:.0f})",
                f"Toplam PnL: <b>{s['pnl']:+.2f} USDT</b> | Ortalama {s['avg_r']:+.2f}R" + (f" | PF {pf:.2f}" if pf else ""),
                f"Komisyon + funding: {s['fees']:.2f} USDT",
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
            lines.append(f"Sinyal: {setups['found']}, alınan: {setups['taken']}{skip}")
        return lines

    def news_text(self) -> str:
        now = now_utc()
        self.news.refresh()
        evs = self.news.upcoming(now, 72)
        if not self.news.has_data:
            return "⚠️ Haber verisi alınamadı."
        if not evs:
            return "📰 Önümüzdeki 72 saatte filtreye takılan önemli haber yok."
        lines = [f"📰 <b>Yaklaşan haberler</b> ({'/'.join(self.cfg.news.currencies)}, "
                 f"{'/'.join(self.cfg.news.impacts)}) — SMC işlemlerinden önce {self.cfg.news.block_hours_before} saat blok"]
        for e in evs[:15]:
            lines.append(f"• {M.local(from_iso(e['time']), self.tz)} — {e['country']} {e['title']}")
        return "\n".join(lines)

    def close_command(self, args: list[str]) -> str:
        if not args:
            return "Kullanım: /close BTC"
        base = args[0].upper().replace("USDT", "")
        for t in sorted(self._active(), key=lambda x: x.account != "main"):
            if base_of(t.symbol) == base:
                if t.status == "pending":
                    self._b(t).cancel_order(t.symbol, t.entry_order_id)
                    self._finish_cancel(t, "Kullanıcı iptal etti (/close)")
                    return f"✅ {base} bekleyen emri iptal edildi."
                self._close_now(t, "Kullanıcı kapattı (/close)")
                return f"✅ {base} pozisyonu kapatıldı ({M.strategy_title(t.strategy, t.account)})."
        return f"{base} için bot tarafından yönetilen pozisyon yok."
