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
    return f"{m // 60}s {m % 60}dk" if m >= 60 else f"{m}dk"


def local(ts: pd.Timestamp | None, tz: str) -> str:
    if ts is None:
        return "-"
    return ts.tz_convert(tz).strftime("%d.%m %H:%M") + ("" if tz == "UTC" else f" ({tz.split('/')[-1]})")


def order_placed(t: Trade, tz: str, kind: str = "limit") -> str:
    reasons = "\n".join(f"• {escape(r)}" for r in t.reasons)
    rr = abs(t.tp - t.entry) / abs(t.entry - t.sl) if t.entry != t.sl else 0
    return (
        f"{_side(t.side)} <b>YENİ İŞLEM — {base_of(t.symbol)}</b> ({kind} emir)\n"
        f"Giriş: <code>{fmt_price(t.entry)}</code>  SL: <code>{fmt_price(t.sl)}</code>  TP: <code>{fmt_price(t.tp)}</code>\n"
        f"R:R 1:{rr:.1f} | Risk %{t.risk_pct:.2f} ({fmt_usd(t.risk_usd)}) | Kaldıraç {t.leverage}x\n"
        f"Skor: <b>{t.score}/100</b> | Bölge: {escape(t.poi)} | Miktar: {t.qty:g}\n"
        f"<b>Neden?</b>\n{reasons}"
        + (f"\nEmir geçerliliği: {local(from_iso(t.expires_at), tz)}" if kind == "limit" else "")
    )


def filled(t: Trade) -> str:
    return (
        f"✅ <b>{base_of(t.symbol)} {t.side.upper()} açıldı</b> @ <code>{fmt_price(t.fill_price)}</code>\n"
        f"Miktar {t.filled_qty:g} | SL <code>{fmt_price(t.sl)}</code> | TP <code>{fmt_price(t.tp)}</code> (borsaya kondu)"
    )


def cancelled(t: Trade, reason: str) -> str:
    return f"⌛ {base_of(t.symbol)} {t.side.upper()} limit emir iptal: {escape(reason)}"


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
    return (
        f"{_side(t.side)} <b>{base_of(t.symbol)}</b> x{qty:g} @ <code>{fmt_price(t.open_price)}</code>\n"
        f"   Fiyat <code>{fmt_price(price)}</code> | PnL <b>{pnl:+.2f} USDT</b> ({t.r_at(price or t.open_price):+.2f}R)\n"
        f"   SL <code>{fmt_price(t.sl)}</code> ({t.r_at(t.sl):+.2f}R) | TP <code>{fmt_price(t.tp)}</code> "
        f"({t.r_at(t.tp):.1f}R){flags}\n"
        f"   Süre {_dur(age)} | Skor {t.score} | {escape(t.poi)}"
    )


HELP = (
    "<b>Komutlar</b>\n"
    "/positions — açık pozisyonlar ve bekleyen emirler\n"
    "/status — bot durumu, bakiye, risk limitleri\n"
    "/stats — kâr/zarar, kazanma oranı, en iyi/kötü coin\n"
    "/news — yaklaşan önemli haberler\n"
    "/pause — yeni işlem açmayı durdur\n"
    "/resume — devam et (risk molalarını da sıfırlar)\n"
    "/close BTC — BTC pozisyonunu/emrini kapat\n"
)
