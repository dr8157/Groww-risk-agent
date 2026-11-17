"""Alerts (Telegram) and dead-man heartbeat (healthchecks.io or any ping URL).

Both run on a background thread so a slow Telegram API can never delay an exit.
Configure in .env:
  TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
  HEARTBEAT_URL=https://hc-ping.com/<uuid>   (alerts YOU if the agent goes silent)
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("notify")
ICON = {"INFO": "ℹ️", "WARN": "⚠️", "LOCK": "🔒", "EXIT": "✂️", "CRITICAL": "🚨", "ERROR": "❗"}


class Notifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.hb_url = os.environ.get("HEARTBEAT_URL", "").strip()
        self.q: queue.Queue = queue.Queue(maxsize=500)
        self._recent: dict[str, float] = {}
        self._last_hb = 0.0
        threading.Thread(target=self._worker, daemon=True, name="notifier").start()

    def send(self, level: str, msg: str, dedupe_s: float = 60):
        """Fire-and-forget. Identical messages within dedupe_s are dropped (CRITICAL never)."""
        k = f"{level}:{msg}"
        now = time.time()
        if level != "CRITICAL" and now - self._recent.get(k, 0) < dedupe_s:
            return
        self._recent[k] = now
        try:
            self.q.put_nowait(("tg", f"{ICON.get(level, '')} {msg}"))
        except queue.Full:
            pass

    def heartbeat(self, ok: bool = True, every_s: float = 60):
        if not self.hb_url or time.time() - self._last_hb < every_s:
            return
        self._last_hb = time.time()
        try:
            self.q.put_nowait(("hb", self.hb_url + ("" if ok else "/fail")))
        except queue.Full:
            pass

    def _worker(self):
        while True:
            kind, payload = self.q.get()
            for attempt in range(3):
                try:
                    if kind == "tg":
                        if not (self.token and self.chat):
                            break
                        data = urllib.parse.urlencode({"chat_id": self.chat, "text": payload[:4000]}).encode()
                        urllib.request.urlopen(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                               data=data, timeout=5).read()
                    else:
                        urllib.request.urlopen(payload, timeout=5).read()
                    break
                except Exception as e:
                    log.warning("notify failed (%s), attempt %d", e, attempt + 1)
                    time.sleep(2 ** attempt)
