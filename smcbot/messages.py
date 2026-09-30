"""Telegram message templates (Turkish, HTML)."""
from __future__ import annotations

from html import escape

import pandas as pd

from .trade import Trade
from .utils import base_of, fmt_price, fmt_usd, from_iso


def _side(t_side: str) -> str:
    return "🟢 LONG" if t_side == "long" else "🔴 SHORT"


def _dur(minutes: float) -> str:
    m = int(max(minutes, 0))
    if m >= 1440:
        return f"{m // 1440}g {m % 1440 // 60}s"
    return f"{m // 60}s {m % 60}dk" if m >= 60 else f"{m}dk"


def local(ts: pd.Timestamp | None, tz: str) -> str:
    if ts is None:
        return "-"
    return ts.tz_convert(tz).strftime("%d.%m %H:%M") + ("" if tz == "UTC" else f" ({tz.split('/')[-1]})")


def strategy_title(strategy: str, account: str) -> str:
    name = "📈 TREND" if strategy == "trend" else "🎯 SMC"
    return f"👁 GÖZLEM {name.split(' ')[1]} (sanal)" if account == "shadow" else name


def tag(t: Trade) -> str:
    return f"<i>{strategy_title(t.strategy, t.account)}</i>"


def _tp_text(t: Trade) -> str:
    return "yok (iz süren stop)" if t.tp is None else f"<code>{fmt_price(t.tp)}</code>"


def order_placed(t: Trade, tz: str, kind: str = "limit") -> str:
    reasons = "\n".join(f"• {escape(r)}" for r in t.reasons)
    rr = f"R:R 1:{abs(t.tp - t.entry) / abs(t.entry - t.sl):.1f} | " if t.tp is not None and t.entry != t.sl else ""
    score = f"Skor: <b>{t.score}/100</b> | " if t.strategy == "smc" else ""
    return (
        f"{_side(t.side)} <b>YENİ İŞLEM — {base_of(t.symbol)}</b> ({kind} emir)\n"
        f"Giriş: <code>{fmt_price(t.entry)}</code>  SL: <code>{fmt_price(t.sl)}</code>  TP: {_tp_text(t)}\n"
        f"{rr}Risk %{t.risk_pct:.2f} ({fmt_usd(t.risk_usd)}) | Kaldıraç {t.leverage}x\n"
        f"{score}Bölge: {escape(t.poi)} | Miktar: {t.qty:g}\n"
        f"<b>Neden?</b>\n{reasons}"
        + (f"\nEmir geçerliliği: {local(from_iso(t.expires_at), tz)}" if kind == "limit" else "")
    )


def filled(t: Trade) -> str:
    return (
        f"✅ <b>{base_of(t.symbol)} {t.side.upper()} açıldı</b> @ <code>{fmt_price(t.fill_price)}</code>\n"
        f"Miktar {t.filled_qty:g} | SL <code>{fmt_price(t.sl)}</code> | TP {_tp_text(t)} (borsaya kondu)"
    )


def cancelled(t: Trade, reason: str) -> str:
    return f"⌛ {base_of(t.symbol)} {t.side.upper()} emir iptal: {escape(reason)}"


def closed(t: Trade) -> str:
    ok = (t.pnl or 0) >= 0
    held = ""
    if t.filled_at and t.closed_at:
        held = " | Süre " + _dur((from_iso(t.closed_at) - from_iso(t.filled_at)).total_seconds() / 60)
    return (
        f"🏁 <b>{base_of(t.symbol)} {t.side.upper()} kapandı</b> — {escape(t.exit_reason or '')} {'✅' if ok else '❌'}\n"
        f"Giriş <code>{fmt_price(t.fill_price)}</code> → Çıkış <code>{fmt_price(t.exit_price)}</code>\n"
        f"PnL: <b>{(t.pnl or 0):+.2f} USDT</b> ({(t.r_multiple or 0):+.2f}R){held}"
    )


def sl_moved(t: Trade, old: float, reason: str) -> str:
    return (
        f"🛡️ <b>{base_of(t.symbol)} {t.side.upper()} — stop güncellendi</b>\n"
        f"SL <code>{fmt_price(old)}</code> → <code>{fmt_price(t.sl)}</code> ({t.r_at(t.sl):+.2f}R kilitli)\n"
        f"Sebep: {escape(reason)}"
    )


def tp_moved(t: Trade, old: float, reason: str) -> str:
    return (
        f"🎯 <b>{base_of(t.symbol)} {t.side.upper()} — hedef uzatıldı</b>\n"
        f"TP <code>{fmt_price(old)}</code> → <code>{fmt_price(t.tp)}</code> ({t.r_at(t.tp):.2f}R)\n"
        f"Sebep: {escape(reason)}"
    )


def early_exit(t: Trade, reason: str) -> str:
    return f"⚡ <b>{base_of(t.symbol)} {t.side.upper()} — anında çıkış</b>\nSebep: {escape(reason)}"


def position_line(t: Trade, price: float | None, now: pd.Timestamp) -> str:
    if t.status == "pending":
        left = (from_iso(t.expires_at) - now).total_seconds() / 60
        return (f"⏳ {base_of(t.symbol)} {t.side.upper()} limit @ <code>{fmt_price(t.entry)}</code> "
                f"(skor {t.score}, {_dur(left)} kaldı)")
    qty = t.filled_qty or t.qty
    pnl = t.dir * ((price or t.open_price) - t.open_price) * qty
    age = (now - from_iso(t.filled_at)).total_seconds() / 60 if t.filled_at else 0
    flags = (" BE✅" if t.be_done else "") + (" TP↗" if t.tp_extended else "")
    tp = "TP yok (iz süren)" if t.tp is None else f"TP <code>{fmt_price(t.tp)}</code> ({t.r_at(t.tp):.1f}R)"
    extra = f"Skor {t.score} | " if t.strategy == "smc" else ""
    return (
        f"{_side(t.side)} <b>{base_of(t.symbol)}</b> x{qty:g} @ <code>{fmt_price(t.open_price)}</code>\n"
        f"   Fiyat <code>{fmt_price(price)}</code> | PnL <b>{pnl:+.2f} USDT</b> ({t.r_at(price or t.open_price):+.2f}R)\n"
        f"   SL <code>{fmt_price(t.sl)}</code> ({t.r_at(t.sl):+.2f}R) | {tp}{flags}\n"
        f"   Süre {_dur(age)} | {extra}{escape(t.poi)}"
    )


HELP = (
    "<b>Komutlar</b>\n"
    "/positions — açık pozisyonlar (trend + gözlem)\n"
    "/status — bot durumu, bakiyeler, risk limitleri\n"
    "/stats — kâr/zarar, kazanma oranı, en iyi/kötü coin\n"
    "/news — yaklaşan önemli haberler\n"
    "/pause — yeni işlem açmayı durdur\n"
    "/resume — devam et (risk molalarını da sıfırlar)\n"
    "/close BTC — BTC pozisyonunu/emrini kapat\n"
)
