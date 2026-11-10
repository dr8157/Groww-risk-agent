"""Client-side rate limiting mirroring Groww's per-type limits, with headroom.

Groww limits are per *type* (all APIs in a type share the budget):
  Orders       10/s  250/min
  Live data    10/s  300/min
  Non-trading  20/s  500/min  (positions, orders list, holdings, margin)
  Auth          5/s   30/min  + 150 tokens / 24h
We run at ~80% so a burst never trips a 429 at the moment we most need to exit.
Exits get priority: they may use the reserved 20% headroom; monitoring may not.
"""
from __future__ import annotations

import threading
import time
from collections import deque

LIMITS = {  # (per_second, per_minute) at 100%
    "orders": (10, 250),
    "live": (10, 300),
    "nontrading": (20, 500),
    "auth": (5, 30),
}
NORMAL_SHARE = 0.8


class SlidingWindowLimiter:
    def __init__(self, per_sec: int, per_min: int):
        self.per_sec, self.per_min = per_sec, per_min
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()
        self.throttled = 0

    def _counts(self, now):
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        last_sec = sum(1 for t in self.calls if now - t < 1)
        return last_sec, len(self.calls)

    def acquire(self, priority: bool = False, timeout: float = 10.0) -> bool:
        share = 1.0 if priority else NORMAL_SHARE
        max_s = max(1, int(self.per_sec * share))
        max_m = max(1, int(self.per_min * share))
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                now = time.monotonic()
                s, m = self._counts(now)
                if s < max_s and m < max_m:
                    self.calls.append(now)
                    return True
                self.throttled += 1
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def usage(self):
        with self.lock:
            s, m = self._counts(time.monotonic())
            return {"last_sec": s, "last_min": m, "per_min_limit": self.per_min, "throttled": self.throttled}


class RateLimits:
    def __init__(self):
        self.buckets = {k: SlidingWindowLimiter(*v) for k, v in LIMITS.items()}

    def acquire(self, kind: str, priority: bool = False, timeout: float = 10.0) -> bool:
        return self.buckets[kind].acquire(priority=priority, timeout=timeout)

    def usage(self):
        return {k: b.usage() for k, b in self.buckets.items()}
