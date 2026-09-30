"""Telegram notifications and commands (plain Bot API over HTTPS, long polling)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str, chat_id: str, enabled: bool = True, kv=None):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else ""
        self.enabled = bool(enabled and token and chat_id)
        self.kv = kv
        self._stop = threading.Event()
        if enabled and not self.enabled:
            log.warning("Telegram devre dışı: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID eksik")

    def _call(self, method: str, http_timeout: float = 15, **payload):
        r = requests.post(API.format(token=self.token, method=method), json=payload, timeout=http_timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram hatası"))
        return data["result"]

    def send(self, text: str) -> None:
        if not self.enabled:
            log.info("[TG] %s", text.replace("\n", " | "))
            return
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]:
            for attempt in range(3):
                try:
                    self._call("sendMessage", chat_id=self.chat_id, text=chunk, parse_mode="HTML",
                               disable_web_page_preview=True)
                    break
                except Exception as e:
                    if attempt == 2:
                        log.warning("Telegram mesajı gönderilemedi: %s", e)
                    time.sleep(1 + attempt)

    # ------------------------------------------------------------------
    def start_commands(self, handler: Callable[[str, list[str]], str]) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self._poll, args=(handler,), daemon=True, name="telegram").start()

    def stop(self) -> None:
        self._stop.set()

    def _poll(self, handler) -> None:
        offset = (self.kv.get_json("tg:offset") if self.kv else None) or 0
        commands = [
            {"command": "positions", "description": "Açık pozisyonlar ve bekleyen emirler"},
            {"command": "status", "description": "Bot durumu, bakiye, risk"},
            {"command": "stats", "description": "Kâr/zarar ve performans"},
            {"command": "news", "description": "Yaklaşan önemli haberler"},
            {"command": "pause", "description": "Yeni işlem açmayı durdur"},
            {"command": "resume", "description": "Yeni işlem açmaya devam et"},
            {"command": "close", "description": "/close BTC - pozisyonu kapat"},
            {"command": "help", "description": "Komut listesi"},
        ]
        try:
            self._call("setMyCommands", commands=commands)
        except Exception as e:
            log.debug("setMyCommands: %s", e)
        while not self._stop.is_set():
            try:
                updates = self._call("getUpdates", http_timeout=40, offset=offset, timeout=30)
            except Exception as e:
                log.debug("getUpdates: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if self.kv:
                    self.kv.set_json("tg:offset", offset)
                msg = u.get("message") or u.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat = str((msg.get("chat") or {}).get("id", ""))
                if not text.startswith("/"):
                    continue
                if chat != self.chat_id:
                    log.warning("Yetkisiz Telegram komutu (chat %s): %s", chat, text)
                    continue
                parts = text.split()
                cmd = parts[0][1:].split("@")[0].lower()
                try:
                    reply = handler(cmd, parts[1:])
                except Exception as e:
                    log.exception("Komut hatası: %s", text)
                    reply = f"⚠️ Komut hatası: {e}"
                if reply:
                    self.send(reply)
