"""Economic calendar filter (ForexFactory weekly JSON feed)."""
from __future__ import annotations

import logging

import pandas as pd
import requests

from .utils import from_iso, now_utc, to_iso

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (smcbot news filter)"}


class NewsCalendar:
    KEY = "news:cache"

    def __init__(self, news_cfg, kv):
        self.cfg = news_cfg
        self.kv = kv
        self.events: list[dict] = []
        self.fetched_at: pd.Timestamp | None = None
        cached = kv.get_json(self.KEY)
        if cached:
            self.events = cached.get("events", [])
            self.fetched_at = from_iso(cached.get("fetched_at"))

    # ------------------------------------------------------------------
    def refresh(self, force: bool = False) -> bool:
        if not self.cfg.enabled:
            return True
        now = now_utc()
        if not force and self.fetched_at is not None and now - self.fetched_at < pd.Timedelta(minutes=self.cfg.refresh_minutes):
            return True
        # after a failure (e.g. HTTP 429) wait 15 minutes instead of retrying every scan
        failed_at = from_iso((self.kv.get_json("news:failed_at") or {}).get("ts"))
        if not force and failed_at is not None and now - failed_at < pd.Timedelta(minutes=15):
            return False
        events: list[dict] = []
        ok = False
        for url, required in ((self.cfg.url_this_week, True), (self.cfg.url_next_week, False)):
            try:
                resp = requests.get(url, headers=HEADERS, timeout=20)
                if resp.status_code != 200:
                    if required:
                        log.warning("Haber takvimi alınamadı: HTTP %s", resp.status_code)
                    continue
                events.extend(self._parse(resp.json()))
                ok = ok or required
            except Exception as e:  # network / JSON errors must never stop the bot
                if required:
                    log.warning("Haber takvimi hatası: %s", e)
        if ok:
            seen = set()
            uniq = []
            for ev in sorted(events, key=lambda x: x["time"]):
                key = (ev["time"], ev["title"], ev["country"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(ev)
            self.events = uniq
            self.fetched_at = now
            self.kv.set_json(self.KEY, {"fetched_at": to_iso(now), "events": uniq})
        else:
            self.kv.set_json("news:failed_at", {"ts": to_iso(now)}, ttl=3600)
        return ok

    @staticmethod
    def _parse(raw: list) -> list[dict]:
        out = []
        for item in raw:
            try:
                ts = pd.Timestamp(item["date"]).tz_convert("UTC")
            except Exception:
                continue
            out.append({
                "time": to_iso(ts),
                "title": item.get("title", ""),
                "country": item.get("country", ""),
                "impact": item.get("impact", ""),
                "forecast": item.get("forecast", ""),
                "previous": item.get("previous", ""),
            })
        return out

    # ------------------------------------------------------------------
    def relevant(self) -> list[dict]:
        cur = set(self.cfg.currencies)
        imp = set(self.cfg.impacts)
        return [e for e in self.events if e["country"] in cur and e["impact"] in imp]

    @property
    def has_data(self) -> bool:
        return self.fetched_at is not None and now_utc() - self.fetched_at < pd.Timedelta(days=7)

    def blocking_event(self, now: pd.Timestamp) -> dict | None:
        """Event that blocks new entries: within [now - after, now + before]."""
        if not self.cfg.enabled:
            return None
        if not self.has_data:
            if self.cfg.fail_closed:
                return {"time": to_iso(now), "title": "Haber verisi alınamadı (güvenli mod)", "country": "-", "impact": "-"}
            return None
        lo = now - pd.Timedelta(minutes=self.cfg.block_minutes_after)
        hi = now + pd.Timedelta(hours=self.cfg.block_hours_before)
        for ev in self.relevant():
            t = from_iso(ev["time"])
            if lo <= t <= hi:
                return ev
        return None

    def minutes_to_next(self, now: pd.Timestamp) -> float | None:
        if not self.cfg.enabled:
            return None
        for ev in self.relevant():
            t = from_iso(ev["time"])
            if t >= now:
                return (t - now).total_seconds() / 60.0
        return None

    def upcoming(self, now: pd.Timestamp, hours: float = 48) -> list[dict]:
        hi = now + pd.Timedelta(hours=hours)
        return [e for e in self.relevant() if now - pd.Timedelta(minutes=30) <= from_iso(e["time"]) <= hi]
