"""Command line interface.

  python -m smcbot run                         # botu çalıştır (config.yaml / BOT_MODE)
  python -m smcbot scan                        # şu anki setupları göster (emir göndermez)
  python -m smcbot backtest --days 120         # geçmiş veride test
  python -m smcbot check                       # bağlantı kontrolleri
  python -m smcbot news                        # yaklaşan haberler
  python -m smcbot report                      # PostgreSQL'den performans analizi
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import ccxt
import pandas as pd

from .config import load_config
from .logger import setup_logging
from .utils import base_of, ccxt_symbol, fmt_price, now_utc

log = logging.getLogger("smcbot")


def cmd_run(cfg, args):
    from .engine import Engine

    if cfg.mode == "live":
        log.warning("CANLI MOD: gerçek para ile işlem yapılacak")
    Engine(cfg).run(once=args.once)


def cmd_scan(cfg, args):
    from .engine import resolve_universe
    from .market_data import MarketData
    from .strategy import find_setups, prepare

    ex = ccxt.binanceusdm({"enableRateLimit": True})
    ex.load_markets()
    md = MarketData(ex)
    symbols = resolve_universe(ex, cfg)
    since = now_utc() - pd.Timedelta(hours=args.hours)
    preps = {}
    for sym in symbols:
        frames = {"ltf": md.candles(sym, cfg.timeframes.ltf, 700), "mtf": md.candles(sym, cfg.timeframes.mtf, 600),
                  "htf": md.candles(sym, cfg.timeframes.htf, 300)}
        preps[sym] = prepare(frames, cfg)
    btc, eth = ccxt_symbol("BTC", cfg.quote), ccxt_symbol("ETH", cfg.quote)
    total = 0
    for sym, P in preps.items():
        trend = {1: "yukarı", -1: "aşağı", 0: "nötr"}
        h4 = P.htf_trend[-1]
        h1 = P.mtf_trend[-1]
        print(f"{base_of(sym):6s} 4H: {trend.get(int(h4) if h4 == h4 else 0)}  1H: {trend.get(int(h1) if h1 == h1 else 0)}")
        for s in find_setups(sym, P, cfg, preps.get(eth if sym == btc else btc), int(since.value)):
            total += 1
            print(f"   {s.created_at:%m-%d %H:%M} {s.side.upper():5s} skor {s.score:3d}  giriş {fmt_price(s.entry)}  "
                  f"SL {fmt_price(s.sl)}  TP {fmt_price(s.tp)} ({s.tp_r:g}R)  {s.poi}  {', '.join(s.swept)}")
    print(f"\nSon {args.hours} saatte {total} setup bulundu.")


def cmd_backtest(cfg, args):
    from .backtest import load_data, load_data_archive, run_backtest, run_trend_backtest, save_results, summarize, \
        to_journal
    from .market_data import MarketData

    requested = [ccxt_symbol(s.strip().upper(), cfg.quote) for s in args.symbols.split(",")] if args.symbols else None
    symbols = list(requested or [ccxt_symbol(b, cfg.quote) for b in cfg.universe.symbols])
    for sym in (ccxt_symbol("BTC", cfg.quote), ccxt_symbol("ETH", cfg.quote)):
        if sym not in symbols:
            symbols.append(sym)  # SMT references
    end = pd.Timestamp(args.end, tz="UTC") if args.end else now_utc().floor("1D")
    start = end - pd.Timedelta(days=args.days)
    warmup = 90 if args.strategy == "trend" else 45  # daily EMA50 needs a longer warm-up
    log.info("Backtest [%s] %s → %s | %s", args.strategy, start.date(), end.date(),
             ",".join(base_of(s) for s in symbols))
    if args.source == "archive":
        data = load_data_archive(symbols, start, end, warmup_days=warmup)
    else:
        ex = ccxt.binanceusdm({"enableRateLimit": True})
        ex.load_markets()
        data = load_data(MarketData(ex), symbols, start, end, warmup_days=warmup)
    trade_symbols = requested  # BTC/ETH may be loaded only as SMT references
    runs = [True, False] if (args.compare and args.strategy == "smc") else [not args.no_optimizer]
    for use_opt in runs:
        if args.strategy == "trend":
            res = run_trend_backtest(cfg, data, start, end, start_balance=args.balance, trade_symbols=trade_symbols)
        else:
            res = run_backtest(cfg, data, start, end, use_optimizer=use_opt, start_balance=args.balance,
                               trade_symbols=trade_symbols)
        summary = summarize(res)
        path = save_results(res, summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        print(f"Sonuçlar: {path}")
        if args.save_db:
            from .storage.db import Journal

            j = Journal(cfg.secrets.database_url, f"backtest-{pd.Timestamp.now():%Y%m%d%H%M}{'' if use_opt else '-noopt'}")
            if j.enabled:
                to_journal(j, res)
                print("Backtest işlemleri PostgreSQL'e yazıldı")


def cmd_check(cfg, args):
    from .engine import make_broker
    from .news import NewsCalendar
    from .storage.db import Journal
    from .storage.kv import make_kv

    ok = True
    kv = make_kv(cfg.secrets.redis_url, cfg.storage.redis_prefix, cfg.storage.state_file)
    print(f"[state]    {kv.backend}")
    j = Journal(cfg.secrets.database_url, cfg.mode)
    print(f"[postgres] {'OK' if j.enabled else 'yok (CSV kullanılacak)'}")
    try:
        b = make_broker(cfg, kv)
        print(f"[binance]  mod={cfg.mode} bakiye={b.equity():.2f} USDT, açık pozisyon={len(b.positions())}")
    except Exception as e:
        ok = False
        print(f"[binance]  HATA: {e}")
    n = NewsCalendar(cfg.news, kv)
    fresh = n.refresh()  # uses the Redis cache when it is < refresh_minutes old (the feed rate-limits)
    age = f"{(pd.Timestamp.now(tz='UTC') - n.fetched_at).total_seconds() / 60:.0f} dk önce alındı" \
        if n.fetched_at is not None else "hiç alınamadı"
    status = "OK" if n.has_data else "HATA"
    note = "" if fresh else " — site şu an yanıt vermedi, önbellek kullanılıyor"
    print(f"[haber]    {status} ({len(n.relevant())} filtreli etkinlik, {age}){note}")
    ok = check_telegram(cfg) and ok
    sys.exit(0 if ok else 1)


def check_telegram(cfg) -> bool:
    """Validate the bot token, help find the chat id, send a test message."""
    from .telegram import Telegram

    token, chat_id = cfg.secrets.telegram_token, cfg.secrets.telegram_chat_id
    if not token:
        print("[telegram] TELEGRAM_BOT_TOKEN boş -> @BotFather'da /newbot ile bot oluşturup token'ı .env'e yazın")
        return True
    tg = Telegram(token, chat_id or "0", True)
    try:
        me = tg._call("getMe")
    except Exception as e:
        print(f"[telegram] HATA: token geçersiz ({e})")
        return False
    print(f"[telegram] bot bulundu: @{me.get('username')}")
    if not chat_id:
        try:
            updates = tg._call("getUpdates")
        except Exception as e:
            print(f"[telegram] mesajlar okunamadı: {e}")
            return False
        chats = {}
        for u in updates:
            chat = (u.get("message") or u.get("edited_message") or {}).get("chat") or {}
            if chat.get("id") is not None:
                chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
        if not chats:
            print(f"[telegram] TELEGRAM_CHAT_ID boş. Telegram'da @{me.get('username')} botunu açıp /start yazın,"
                  " sonra bu komutu tekrar çalıştırın.")
        else:
            for cid, name in chats.items():
                print(f"[telegram] bulunan chat: {name} -> .env içine yazın:  TELEGRAM_CHAT_ID={cid}")
        return True
    try:
        tg._call("sendMessage", chat_id=chat_id, text="✅ SMC Bot bağlantı testi başarılı")
        print("[telegram] test mesajı gönderildi, Telegram'ı kontrol edin")
        return True
    except Exception as e:
        print(f"[telegram] HATA: mesaj gönderilemedi ({e}). Chat ID doğru mu? Bota /start yazdınız mı?")
        return False


def cmd_news(cfg, args):
    from .news import NewsCalendar
    from .storage.kv import MemoryKV

    n = NewsCalendar(cfg.news, MemoryKV())
    n.refresh(force=True)
    now = now_utc()
    ev = n.blocking_event(now)
    print("Şu an yeni işlem:", f"ENGELLİ ({ev['title']})" if ev else "serbest")
    for e in n.upcoming(now, args.hours):
        print(f"  {pd.Timestamp(e['time']):%a %d.%m %H:%M} UTC  {e['country']:4s} {e['impact']:6s} {e['title']}")


def cmd_report(cfg, args):
    from .storage.db import Journal

    j = Journal(cfg.secrets.database_url, cfg.mode)
    if not j.enabled:
        print("PostgreSQL bağlantısı yok (DATABASE_URL). CSV: state/trades.csv")
        return
    for view in ("v_summary", "v_by_symbol", "v_by_score", "v_by_exit_reason", "v_by_poi", "v_by_swept_level",
                 "v_by_feature", "v_by_hour", "v_optimizer_effect", "v_rejected_setups"):
        rows = j.query(f"SELECT * FROM {view}" + (" WHERE mode = %s" if args.mode else ""),
                       (args.mode,) if args.mode else None)
        print(f"\n=== {view} ===")
        if rows:
            print(pd.DataFrame(rows).to_string(index=False))
        else:
            print("(veri yok)")


def apply_override(cfg, item: str) -> None:
    import yaml

    from .config import _enforce_limits, _wrap

    key, _, raw = item.partition("=")
    node = cfg
    parts = key.strip().split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = _wrap(yaml.safe_load(raw))
    _enforce_limits(cfg)


def main():
    p = argparse.ArgumentParser(prog="smcbot", description="SMC trading bot")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    rp_ = sub.add_parser("run")
    rp_.add_argument("--once", action="store_true", help="tek döngü çalıştır ve çık")
    sp = sub.add_parser("scan")
    sp.add_argument("--hours", type=float, default=24)
    bp = sub.add_parser("backtest")
    bp.add_argument("--days", type=int, default=120)
    bp.add_argument("--strategy", choices=["trend", "smc"], default="trend")
    bp.add_argument("--source", choices=["api", "archive"], default="api",
                    help="api = Binance API (ccxt) | archive = data.binance.vision (hızlı, funding dahil)")
    bp.add_argument("--end", default=None, help="YYYY-MM-DD (varsayılan: bugün)")
    bp.add_argument("--symbols", default=None, help="BTC,ETH,... (varsayılan: config)")
    bp.add_argument("--balance", type=float, default=1000.0)
    bp.add_argument("--no-optimizer", action="store_true")
    bp.add_argument("--compare", action="store_true", help="optimizasyonlu ve optimizasyonsuz karşılaştır")
    bp.add_argument("--save-db", action="store_true", help="işlemleri PostgreSQL'e yaz")
    bp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="ayar ezme, örn: --set strategy.entry_mode=limit --set risk.base_rr=1.5")
    sub.add_parser("check")
    np_ = sub.add_parser("news")
    np_.add_argument("--hours", type=float, default=72)
    rp = sub.add_parser("report")
    rp.add_argument("--mode", default=None)
    args = p.parse_args()

    cfg = load_config(args.config)
    for item in getattr(args, "set", []) or []:
        apply_override(cfg, item)
    setup_logging(cfg.logging.level, cfg.logging.file if args.cmd == "run" else None)
    {"run": cmd_run, "scan": cmd_scan, "backtest": cmd_backtest, "check": cmd_check, "news": cmd_news,
     "report": cmd_report}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
