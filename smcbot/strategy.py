"""Setup detection.

Model (per direction, long shown; shorts use the same code on price-negated data):

  1. Liquidity sweep : a 15m candle trades below a sell-side liquidity level (PDL, PWL, EQL,
                       1H/15m swing low, Asia low) and closes back above it within 2 candles.
  2. MSS             : after the sweep, a candle closes above the internal swing high that
                       formed the swept low (market structure shift) within 4 hours, with
                       displacement (a candle body >= 1 ATR) and without a new low.
  3. POI             : an unfilled bullish FVG in the displacement leg (preferably overlapping
                       the order block), else the order block itself. The POI closest to the
                       OTE level (70.5% retracement of the leg) is chosen. In ``market`` entry
                       mode (default) the trade is entered on the MSS confirmation close and the
                       POI only confirms that the displacement left an imbalance; in ``limit``
                       mode a limit order waits at the POI.
  4. Risk            : SL beyond the sweep extreme + buffer, never tighter than 0.5 x ATR(1H)
                       (keeps trades intraday, >= ~1h), TP = 2R (3R for A+ setups).
  5. Hard filters    : 4H trend aligned, sweep of a significant level (weight >= 10), London/NY
                       killzone (see docs/RESEARCH.md for why).
     Score           : 4H bias, 1H bias, level importance, displacement strength, volume,
                       premium/discount, OTE, killzone, SMT divergence, room to the next
                       opposing liquidity pool.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import atr, sma
from .smc.fvg import bullish_fvg
from .smc.liquidity import Levels, build_levels, first_touch, level_bounds
from .smc.structure import market_structure, pivots
from .utils import index_ns, tf_delta, tf_ns, to_iso


@dataclass
class Setup:
    id: str
    symbol: str
    side: str
    created_at: pd.Timestamp
    expires_at: pd.Timestamp
    entry: float
    sl: float
    tp: float
    tp_r: float
    score: int
    reasons: list[str]
    poi: str
    zone_low: float
    zone_high: float
    sweep_price: float
    swept: list[str]
    dol_rr: float
    targets: list[float]
    atr_ltf: float
    atr_mtf: float
    features: dict = field(default_factory=dict)

    @property
    def dir(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["created_at"] = to_iso(self.created_at)
        d["expires_at"] = to_iso(self.expires_at)
        return d


@dataclass
class Prepared:
    """All per-symbol analysis that setups and the position optimizer need."""

    ltf: pd.DataFrame
    mtf: pd.DataFrame
    htf: pd.DataFrame
    ltf_open_ns: np.ndarray
    ltf_close_ns: np.ndarray
    mtf_close_ns: np.ndarray
    atr_ltf: np.ndarray
    atr_mtf_on_ltf: np.ndarray
    htf_trend: np.ndarray
    mtf_trend: np.ndarray
    range_hi: np.ndarray
    range_lo: np.ndarray
    vol_ratio: np.ndarray
    levels: Levels
    lv_start: np.ndarray
    lv_end: np.ndarray
    taken: np.ndarray
    ms_ltf: pd.DataFrame
    ms_mtf: pd.DataFrame


def _asof(src_close_ns: np.ndarray, values, target_ns: np.ndarray) -> np.ndarray:
    """Value of the last *closed* source bar at each target time."""
    vals = np.asarray(values, dtype=float)
    if len(vals) == 0:
        return np.full(len(target_ns), np.nan)
    idx = np.searchsorted(src_close_ns, target_ns, side="right") - 1
    return np.where(idx >= 0, vals[np.clip(idx, 0, None)], np.nan)


def prepare(frames: dict, cfg) -> Prepared:
    s, tfs = cfg.strategy, cfg.timeframes
    ltf, mtf, htf = frames["ltf"], frames["mtf"], frames["htf"]
    ltf_open = index_ns(ltf.index)
    ltf_close = ltf_open + tf_ns(tfs.ltf)
    mtf_close = index_ns(mtf.index) + tf_ns(tfs.mtf)
    htf_close = index_ns(htf.index) + tf_ns(tfs.htf)

    atr_l = atr(ltf, s.atr_period).to_numpy()
    atr_m = atr(mtf, s.atr_period).to_numpy()
    ms_h = market_structure(htf, s.swing_length_htf)
    ms_m = market_structure(mtf, s.swing_length_mtf)
    ms_l = market_structure(ltf, s.swing_length_ltf)

    levels = build_levels(ltf, mtf, atr_l, atr_m, s.liquidity, tfs.ltf, tfs.mtf)
    start, end = level_bounds(levels, ltf_open)
    taken = first_touch(levels, start, end, ltf["high"].to_numpy(float), ltf["low"].to_numpy(float))
    vol = ltf["volume"].astype(float)
    vol_ratio = (vol / sma(vol, 20).replace(0, np.nan)).to_numpy()

    return Prepared(
        ltf=ltf, mtf=mtf, htf=htf,
        ltf_open_ns=ltf_open, ltf_close_ns=ltf_close, mtf_close_ns=mtf_close,
        atr_ltf=atr_l,
        atr_mtf_on_ltf=_asof(mtf_close, atr_m, ltf_close),
        htf_trend=_asof(htf_close, ms_h["trend"].to_numpy(), ltf_close),
        mtf_trend=_asof(mtf_close, ms_m["trend"].to_numpy(), ltf_close),
        range_hi=_asof(mtf_close, ms_m["sh_price"].to_numpy(), ltf_close),
        range_lo=_asof(mtf_close, ms_m["sl_price"].to_numpy(), ltf_close),
        vol_ratio=vol_ratio,
        levels=levels, lv_start=start, lv_end=end, taken=taken,
        ms_ltf=ms_l, ms_mtf=ms_m,
    )


def find_setups(symbol: str, P: Prepared, cfg, ref: Prepared | None = None,
                start_ns: int | None = None) -> list[Setup]:
    """All setups whose MSS candle closed at or after ``start_ns`` (default: whole history)."""
    ltf = P.ltf
    o = ltf["open"].to_numpy(float)
    h = ltf["high"].to_numpy(float)
    lo = ltf["low"].to_numpy(float)
    c = ltf["close"].to_numpy(float)
    ref_h = ref_l = None
    if ref is not None:
        r = ref.ltf.reindex(ltf.index)
        ref_h, ref_l = r["high"].to_numpy(float), r["low"].to_numpy(float)
    first_idx = 0 if start_ns is None else int(np.searchsorted(P.ltf_close_ns, start_ns, side="left"))

    found: list[Setup] = []
    # long: raw prices / short: negated prices so that one bullish routine serves both sides
    found += _scan(symbol, 1, (o, h, lo, c), P.levels, P.htf_trend, P.mtf_trend,
                   P.range_hi, P.range_lo, ref_l, P, cfg, first_idx)
    found += _scan(symbol, -1, (-o, -lo, -h, -c), P.levels.flipped(), -P.htf_trend, -P.mtf_trend,
                   -P.range_lo, -P.range_hi, None if ref_h is None else -ref_h, P, cfg, first_idx)

    best: dict[str, Setup] = {}
    for st in found:
        if st.id not in best or st.score > best[st.id].score:
            best[st.id] = st
    return sorted(best.values(), key=lambda x: (x.created_at, -x.score))


def _in_killzone(ts: pd.Timestamp, zones) -> bool:
    return any(a <= ts.hour < b for a, b in zones)


def _scan(symbol, d, arr, lv: Levels, ht, mt, rhi, rlo, ref_low, P: Prepared, cfg, first_idx) -> list[Setup]:
    s = cfg.strategy
    o, h, lo, c = arr
    n = len(c)
    if n < 50:
        return []
    atr_l, atr_m = P.atr_ltf, P.atr_mtf_on_ltf
    body = np.abs(c - o)
    bull = c > o
    iph, _ = pivots(h, lo, s.internal_length)
    iph_idx = np.flatnonzero(iph)
    fvg_bot, fvg_top = bullish_fvg(h, lo)
    taken = P.taken
    ltf_index = P.ltf.index
    step = tf_delta(cfg.timeframes.ltf)
    side = "long" if d == 1 else "short"

    # 1) liquidity sweeps, grouped by reclaim candle
    events: dict[int, list[tuple[int, int]]] = {}
    for k in np.flatnonzero(~lv.is_high):
        p = int(taken[k])
        if p >= n or p < 1:
            continue
        if p - int(P.lv_start[k]) < s.sweep.min_level_age_bars:
            continue
        price = lv.price[k]
        if c[p - 1] <= price:  # must approach from above
            continue
        r = None
        for q in range(p, min(p + s.sweep.reclaim_bars + 1, n)):
            if c[q] > price:
                r = q
                break
        if r is None:
            continue
        if price - lo[p:r + 1].min() > s.sweep.max_depth_atr * atr_l[p]:
            continue
        events.setdefault(r, []).append((int(k), p))

    results: list[Setup] = []
    for r in sorted(events):
        ev = events[r]
        p0 = min(p for _, p in ev)
        e = p0 + int(np.argmin(lo[p0:r + 1]))
        ext = lo[e]

        # 2) market structure shift: close above the internal high that produced the sweep
        pos = int(np.searchsorted(iph_idx, e)) - 1
        if pos < 0:
            continue
        j = int(iph_idx[pos])
        if e - j > s.mss_lookback_bars:
            continue
        mss_level = h[j]
        m = None
        for t in range(r, min(e + s.mss_window_bars, n - 1) + 1):
            if t > e and lo[t] < ext:
                break
            if c[t] > mss_level and t >= j + s.internal_length:
                m = t
                break
        if m is None or m < first_idx:
            continue
        a_l, a_m = atr_l[m], atr_m[m]
        if not (np.isfinite(a_l) and np.isfinite(a_m)) or a_l <= 0 or a_m <= 0:
            continue

        # displacement
        bodies = np.where(bull[e:m + 1], body[e:m + 1], 0.0)
        max_body = float(bodies.max())
        disp = max_body / a_l
        if disp < s.displacement_atr:
            continue
        d_idx = e + int(np.argmax(bodies))
        vr = P.vol_ratio[d_idx]
        leg_hi = float(h[e:m + 1].max())
        leg = leg_hi - ext
        if leg <= 0:
            continue
        ote_px = leg_hi - 0.705 * leg

        # 3) POI: order block + FVGs of the displacement leg
        first_disp = e + int(np.argmax(bodies >= 0.5 * max_body))
        ob = None
        ob_k = -1
        for k in range(first_disp - 1, e - 1, -1):
            if c[k] < o[k]:
                ob, ob_k = (lo[k], o[k]), k
                break
        cands = []
        for i in range(e + 2, m + 1):
            if np.isnan(fvg_bot[i]):
                continue
            bot, top = fvg_bot[i], fvg_top[i]
            if top - bot < s.min_fvg_atr * a_l:
                continue
            entry = (bot + top) / 2 if s.entry_at == "ce" else top
            if i < m and lo[i + 1:m + 1].min() <= entry:
                continue
            overlap = ob is not None and bot <= ob[1] and top >= ob[0]
            cands.append((entry, bot, top, "FVG+OB" if overlap else "FVG"))
        if not cands and s.allow_ob_only and ob is not None:
            bot, top = ob
            entry = (bot + top) / 2 if s.entry_at == "ce" else top
            if top > bot and lo[ob_k + 1:m + 1].min() > entry:
                cands.append((entry, bot, top, "OB"))
        if not cands:
            continue
        entry, zlo, zhi, poi = min(cands, key=lambda x: abs(x[0] - ote_px))
        if entry >= c[m]:
            continue
        if s.entry_mode == "market":
            entry = c[m]  # enter on the MSS confirmation close; the POI only confirms the displacement

        # 4) risk
        sl = ext - s.sl_buffer_atr * a_l
        risk = entry - sl
        min_risk = max(s.min_sl_atr_h1 * a_m, s.min_sl_pct / 100.0 * abs(entry))
        if risk < min_risk:
            sl = entry - min_risk
            risk = min_risk
        if risk > s.max_sl_atr_h1 * a_m:
            continue

        # draw on liquidity: nearest untouched buy-side pool above
        active = (lv.is_high & (P.lv_start <= m) & (P.lv_end > m) & (taken > m)
                  & (lv.price > entry) & (lv.weight >= 8))
        targets = np.sort(lv.price[active])
        dol_rr = (targets[0] - entry) / risk if targets.size else np.inf
        if dol_rr < s.min_room_rr:
            continue

        htv, mtv = ht[m], mt[m]
        if s.require_htf_alignment and htv != 1:
            continue
        sweep_w = int(max(lv.weight[k] for k, _ in ev))
        if sweep_w < s.min_sweep_weight:
            continue
        kz = _in_killzone(ltf_index[m], s.killzones_utc) or _in_killzone(ltf_index[e], s.killzones_utc)
        if s.require_killzone and not kz:
            continue

        # 5) confluence score
        score = 0
        reasons: list[str] = []
        if htv == 1:
            score += 20
            reasons.append("4H trend uyumlu")
        elif htv == -1:
            reasons.append("4H trend ters")
        else:
            score += 8
            reasons.append("4H trend nötr")
        if mtv == 1:
            score += 10
            reasons.append("1H trend uyumlu")
        elif mtv != -1:
            score += 4
        swept_labels = sorted({P.levels.label(k) for k, _ in ev})
        score += sweep_w + (3 if len(ev) > 1 else 0)
        reasons.append("Likidite sweep: " + ", ".join(swept_labels))
        same_candle = r == p0
        if same_candle:
            score += 5
            reasons.append("Tek mumda geri alım (turtle soup)")
        if disp >= 1.5:
            score += 10
        else:
            score += 6
        reasons.append(f"Displacement {disp:.1f} ATR")
        if np.isfinite(vr) and vr >= 1.5:
            score += 5
            reasons.append(f"Hacim {vr:.1f}x ortalama")
        if poi == "FVG+OB":
            score += 5
        reasons.append(f"Giriş bölgesi: {poi}")
        discount = False
        if np.isfinite(rhi[m]) and np.isfinite(rlo[m]) and rhi[m] > rlo[m]:
            discount = entry < (rhi[m] + rlo[m]) / 2
        if discount:
            score += 10
            reasons.append("1H " + ("discount" if d == 1 else "premium") + " bölgesi")
        retr = (leg_hi - entry) / leg
        ote = 0.62 <= retr <= 0.79
        if ote:
            score += 5
            reasons.append("OTE (%62-79) seviyesi")
        mss_time = ltf_index[m]
        if kz:
            score += 5
            reasons.append("Killzone (London/NY)")
        smt = False
        W = s.smt_window_bars
        if ref_low is not None and e - W >= 0:
            if lo[e] < lo[e - W:e - 2].min():
                ref_prior = np.nanmin(ref_low[e - W:e - 2]) if np.isfinite(ref_low[e - W:e - 2]).any() else np.nan
                win = ref_low[max(e - 2, 0):min(e + 3, m + 1)]
                ref_now = np.nanmin(win) if np.isfinite(win).any() else np.nan
                smt = bool(np.isfinite(ref_prior) and np.isfinite(ref_now) and ref_now > ref_prior)
        if smt:
            score += 8
            reasons.append("SMT divergence")
        if dol_rr >= 2.0:
            score += 4
        if dol_rr >= 3.0:
            score += 4
        reasons.append("Karşı likidite: " + (f"{dol_rr:.1f}R" if np.isfinite(dol_rr) else "açık alan"))
        score = int(min(score, 100))
        if score < s.min_score:
            continue

        tp_r = cfg.risk.max_rr if (score >= s.a_plus_score and dol_rr >= cfg.risk.max_rr) else cfg.risk.base_rr
        created = ltf_index[m] + step
        real = (lambda x: float(d * x))
        results.append(Setup(
            id=f"{symbol}|{side}|{created.strftime('%Y%m%dT%H%M')}",
            symbol=symbol,
            side=side,
            created_at=created,
            expires_at=created + step * s.entry_expiry_bars,
            entry=real(entry),
            sl=real(sl),
            tp=real(entry + tp_r * risk),
            tp_r=float(tp_r),
            score=score,
            reasons=reasons,
            poi=poi,
            zone_low=min(real(zlo), real(zhi)),
            zone_high=max(real(zlo), real(zhi)),
            sweep_price=real(ext),
            swept=swept_labels,
            dol_rr=float(dol_rr) if np.isfinite(dol_rr) else 99.0,
            targets=[real(x) for x in targets[:6]],
            atr_ltf=float(a_l),
            atr_mtf=float(a_m),
            features={
                "htf_trend": float(htv) if np.isfinite(htv) else 0.0,
                "mtf_trend": float(mtv) if np.isfinite(mtv) else 0.0,
                "sweep_weight": sweep_w,
                "levels_swept": len(ev),
                "same_candle_reclaim": bool(same_candle),
                "displacement_atr": round(float(disp), 3),
                "volume_ratio": round(float(vr), 3) if np.isfinite(vr) else None,
                "discount": bool(discount),
                "retracement": round(float(retr), 3),
                "ote": bool(ote),
                "killzone": bool(kz),
                "smt": smt,
                "dol_rr": round(float(dol_rr), 3) if np.isfinite(dol_rr) else None,
                "risk_atr_h1": round(float(risk / a_m), 3),
                "risk_pct_price": round(float(risk / abs(entry) * 100), 4),
                "entry_mode": s.entry_mode,
                "sweep_time": to_iso(ltf_index[e]),
                "mss_time": to_iso(mss_time),
                "bars_sweep_to_mss": int(m - e),
            },
        ))
    return results
