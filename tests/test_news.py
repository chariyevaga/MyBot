import pandas as pd

from smcbot.news import NewsCalendar
from smcbot.storage.kv import MemoryKV
from smcbot.utils import now_utc, to_iso

RAW = [
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00",
     "impact": "High", "forecast": "", "previous": ""},
    {"title": "German Flash PMI", "country": "EUR", "date": "2026-10-02T03:30:00-04:00",
     "impact": "High", "forecast": "", "previous": ""},
    {"title": "FOMC Member Speaks", "country": "USD", "date": "2026-10-02T10:00:00-04:00",
     "impact": "Low", "forecast": "", "previous": ""},
]


def calendar(cfg):
    n = NewsCalendar(cfg.news, MemoryKV())
    n.events = NewsCalendar._parse(RAW)
    n.fetched_at = now_utc()
    return n


def test_parse_converts_to_utc(cfg):
    n = calendar(cfg)
    nfp = [e for e in n.events if "Non-Farm" in e["title"]][0]
    assert nfp["time"] == "2026-10-02T12:30:00+00:00"


def test_only_relevant_currency_and_impact(cfg):
    titles = [e["title"] for e in calendar(cfg).relevant()]
    assert titles == ["Non-Farm Employment Change"]


def test_blocking_window(cfg):
    n = calendar(cfg)
    nfp = pd.Timestamp("2026-10-02T12:30:00Z")
    assert n.blocking_event(nfp - pd.Timedelta(hours=3, minutes=59)) is not None   # within 4h before
    assert n.blocking_event(nfp - pd.Timedelta(hours=4, minutes=1)) is None
    assert n.blocking_event(nfp + pd.Timedelta(minutes=29)) is not None            # 30 min after
    assert n.blocking_event(nfp + pd.Timedelta(minutes=31)) is None
    assert round(n.minutes_to_next(nfp - pd.Timedelta(minutes=90))) == 90


def test_fail_closed_without_data(cfg):
    n = NewsCalendar(cfg.news, MemoryKV())
    assert n.blocking_event(now_utc()) is not None
    cfg.news.fail_closed = False
    assert NewsCalendar(cfg.news, MemoryKV()).blocking_event(now_utc()) is None


def test_cache_roundtrip(cfg):
    kv = MemoryKV()
    kv.set_json(NewsCalendar.KEY, {"fetched_at": to_iso(now_utc()), "events": NewsCalendar._parse(RAW)})
    assert NewsCalendar(cfg.news, kv).has_data
