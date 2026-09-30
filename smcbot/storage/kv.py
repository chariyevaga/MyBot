"""Short-term state store: Redis, with a JSON-file fallback for running without Docker."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class KV:
    def get_json(self, key: str, default=None): ...
    def set_json(self, key: str, value, ttl: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    backend = "none"


class RedisKV(KV):
    backend = "redis"

    def __init__(self, url: str, prefix: str):
        import redis

        self.r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        self.r.ping()
        self.p = prefix

    def get_json(self, key, default=None):
        raw = self.r.get(self.p + key)
        return json.loads(raw) if raw is not None else default

    def set_json(self, key, value, ttl=None):
        self.r.set(self.p + key, json.dumps(value, default=str), ex=ttl)

    def delete(self, key):
        self.r.delete(self.p + key)

    def exists(self, key):
        return bool(self.r.exists(self.p + key))


class FileKV(KV):
    backend = "file"

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.error("Durum dosyası bozuk, yeni dosya başlatılıyor: %s", self.path)

    def _alive(self, key) -> bool:
        item = self.data.get(key)
        if item is None:
            return False
        exp = item.get("exp")
        if exp is not None and exp < time.time():
            self.data.pop(key, None)
            return False
        return True

    def _flush(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, default=str, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def get_json(self, key, default=None):
        with self.lock:
            return self.data[key]["v"] if self._alive(key) else default

    def set_json(self, key, value, ttl=None):
        with self.lock:
            self.data[key] = {"v": json.loads(json.dumps(value, default=str)),
                              "exp": time.time() + ttl if ttl else None}
            self._flush()

    def delete(self, key):
        with self.lock:
            if self.data.pop(key, None) is not None:
                self._flush()

    def exists(self, key):
        with self.lock:
            return self._alive(key)


class MemoryKV(KV):
    """In-process store (backtests / tests)."""

    backend = "memory"

    def __init__(self):
        self.data: dict = {}

    def get_json(self, key, default=None):
        item = self.data.get(key)
        if item is None or (item[1] is not None and item[1] < time.time()):
            return default
        return item[0]

    def set_json(self, key, value, ttl=None):
        self.data[key] = (value, time.time() + ttl if ttl else None)

    def delete(self, key):
        self.data.pop(key, None)

    def exists(self, key):
        return self.get_json(key) is not None


def make_kv(redis_url: str, prefix: str, fallback_path: str) -> KV:
    if redis_url:
        try:
            kv = RedisKV(redis_url, prefix)
            log.info("Redis bağlandı: %s", redis_url.split("@")[-1])
            return kv
        except Exception as e:
            log.warning("Redis'e bağlanılamadı (%s), dosya tabanlı duruma geçiliyor: %s", e, fallback_path)
    return FileKV(fallback_path)
