"""Broker adapters. The engine only uses this interface:

  positions() -> list[dict]   (Groww position schema + 'segment')
  ltp(positions) -> {"NSE_SYM": px}
  orders_today() -> list[dict] (Groww order schema + 'segment')
  place_order(**kw) -> dict    (groww_order_id, order_status, order_reference_id)
  cancel_order(groww_order_id, segment) -> dict
  maintain()                   (optional) called every engine tick: proactive token refresh

Authentication (unattended): use a Groww *TOTP* API key. The agent generates its own
6-digit codes from the stored secret and mints a fresh access token every day after the
06:00 IST reset. No daily approval click is needed. The "approval" key type (API secret)
needs a manual approval in Groww Cloud every day and is NOT suitable for unattended use.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta

from .config import AGENT_REF_PREFIX, DATA_DIR, IST, TOKEN_RESET, now_ist, read_json, write_json
from .errors import AuthError, BrokerError, RateLimited, RequestRejected
from .ratelimit import RateLimits

log = logging.getLogger("broker")
HTTP_TIMEOUT = 5  # seconds per Groww call
REFRESH_AFTER = dtime(6, 2)        # start minting the day's token a little after the 06:00 reset
CRITICAL_AUTH_AFTER = dtime(8, 30)  # no token by now on a weekday = you are not protected
SEGMENT_RETRY_S = 600              # retry an unavailable segment (e.g. COMMODITY) every 10 minutes
ALERT_EVERY_S = 1800

__all__ = ["BrokerError", "AuthError", "RateLimited", "RequestRejected", "GrowwBroker", "MockBroker"]


class GrowwBroker:
    SEGMENTS = ["CASH", "FNO", "COMMODITY"]
    CORE_SEGMENTS = {"CASH", "FNO"}   # a failure here is fatal; others degrade with an alert
    MAX_TOKENS_PER_DAY = 100          # Groww allows 150/24h; keep a margin

    def __init__(self, limits: RateLimits | None = None, on_alert=None):
        from growwapi import GrowwAPI
        from growwapi.groww import exceptions as gx
        self._G, self._gx = GrowwAPI, gx
        self.limits = limits or RateLimits()
        self.on_alert = on_alert or (lambda level, msg: None)
        self.client = None
        self.segments = list(self.SEGMENTS)
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="groww")
        self._auth_lock = threading.Lock()
        self._gen = 0                    # bumped on every successful login
        self._minted_at: datetime | None = None
        self._next_auth_at = 0.0
        self._fail_backoff = 5.0
        self._last_auth_alert = 0.0
        self.auth_error: str | None = None
        self._seg_down: dict[str, float] = {}   # segment -> retry-at (monotonic)
        self.ltp_errors = 0
        try:
            self.auth()
        except AuthError as e:  # never crash on a failed login: keep retrying with backoff
            self._alert_auth(str(e))

    # ---------------- auth
    @staticmethod
    def token_expired_at(minted_at: datetime | None, now: datetime) -> bool:
        """Groww tokens die at 06:00 IST. A token minted before the most recent 06:00 is dead."""
        if minted_at is None:
            return True
        reset = now.replace(hour=TOKEN_RESET.hour, minute=TOKEN_RESET.minute, second=0, microsecond=0)
        if now < reset:
            reset -= timedelta(days=1)
        return minted_at < reset

    def _tokens_today(self) -> int:
        return read_json(DATA_DIR / "token_counter.json", {}).get(now_ist().date().isoformat(), 0)

    def _count_token(self):
        today = now_ist().date().isoformat()
        write_json(DATA_DIR / "token_counter.json", {today: self._tokens_today() + 1})

    def auth(self, seen_gen: int | None = None):
        """Log in. Safe to call from many threads at once: if someone else already minted a
        newer token than the one the caller failed with (seen_gen), this is a no-op.
        Failed attempts back off exponentially (5s .. 5min) and do NOT use up the daily budget."""
        with self._auth_lock:
            if seen_gen is not None and seen_gen != self._gen:
                return
            if time.time() < self._next_auth_at:
                raise AuthError(f"login backing off after: {self.auth_error}")
            if self._tokens_today() >= self.MAX_TOKENS_PER_DAY:
                raise AuthError("daily token budget used up - not requesting more tokens today")
            self.limits.acquire("auth", priority=True)
            key = os.environ["GROWW_API_KEY"]
            try:
                if os.environ.get("GROWW_TOTP_SECRET"):
                    import pyotp
                    token = self._G.get_access_token(
                        api_key=key, totp=pyotp.TOTP(os.environ["GROWW_TOTP_SECRET"].strip()).now())
                else:
                    log.warning("using the API-secret (approval) flow: Groww needs a manual approval "
                                "every day. Switch to a TOTP key for unattended operation.")
                    token = self._G.get_access_token(api_key=key, secret=os.environ["GROWW_API_SECRET"])
            except Exception as e:
                self.auth_error = f"{type(e).__name__}: {e}"
                self._next_auth_at = time.time() + self._fail_backoff
                self._fail_backoff = min(self._fail_backoff * 2, 300)
                raise AuthError(f"login failed: {self.auth_error}") from e
            self.client = self._G(token if isinstance(token, str) else token.get("token"))
            self._gen += 1
            self._minted_at = now_ist()
            self._count_token()
            self._fail_backoff, self._next_auth_at = 5.0, 0.0
            if self.auth_error:
                self.on_alert("WARN", "Groww login recovered - protection active again.")
            self.auth_error = None
            log.info("authenticated with Groww (token #%d today)", self._tokens_today())

    def _alert_auth(self, msg: str):
        if time.time() - self._last_auth_alert < ALERT_EVERY_S:
            return
        self._last_auth_alert = time.time()
        n = now_ist()
        loud = n.weekday() < 5 and CRITICAL_AUTH_AFTER <= n.time() <= dtime(23, 55)
        self.on_alert("CRITICAL" if loud else "WARN",
                      f"Groww login is failing ({msg}). "
                      + ("You are NOT protected. Check the TOTP key/secret on the server." if loud
                         else "Will keep retrying."))

    def maintain(self):
        """Called every engine tick. Mints the day's token right after the 06:00 reset,
        so the first call of the day never has to fail with a 401 first."""
        n = now_ist()
        if self.client is not None and not self.token_expired_at(self._minted_at, n):
            return
        if self.client is not None and TOKEN_RESET <= n.time() < REFRESH_AFTER:
            return  # give Groww a moment after the reset
        try:
            self.auth()
        except AuthError as e:
            self._alert_auth(str(e))

    def auth_status(self) -> dict:
        return {"token_minted_at": self._minted_at.isoformat() if self._minted_at else None,
                "token_ok": self.client is not None and not self.token_expired_at(self._minted_at, now_ist()),
                "tokens_used_today": self._tokens_today(), "last_error": self.auth_error,
                "segments_down": sorted(self._seg_down)}

    # ---------------- call wrapper: rate limit + error mapping + one re-auth
    def _call(self, kind: str, fn_name: str, priority=False, **kw):
        gx = self._gx
        for attempt in (1, 2):
            if self.client is None:
                self.auth()
            gen = self._gen
            if not self.limits.acquire(kind, priority=priority, timeout=10):
                raise RateLimited(f"local limiter saturated for {kind}")
            try:
                return getattr(self.client, fn_name)(timeout=HTTP_TIMEOUT, **kw)
            except (gx.GrowwAPIAuthenticationException, gx.GrowwAPIAuthorisationException) as e:
                if attempt == 2:
                    raise AuthError(str(e)) from e
                self.auth(seen_gen=gen)
            except gx.GrowwAPIRateLimitException as e:
                time.sleep(1.0)
                if attempt == 2:
                    raise RateLimited(str(e)) from e
            except gx.GrowwAPITimeoutException as e:
                if attempt == 2:
                    raise BrokerError(f"timeout: {fn_name}") from e
            except (gx.GrowwAPIBadRequestException, gx.GrowwAPINotFoundException) as e:
                raise RequestRejected(f"{fn_name}: {e}") from e
            except BrokerError:
                raise
            except Exception as e:
                raise BrokerError(f"{fn_name}: {type(e).__name__}: {e}") from e

    # ---------------- reads
    def _segment_unavailable(self, seg: str, err: Exception):
        first = seg not in self._seg_down
        self._seg_down[seg] = time.monotonic() + SEGMENT_RETRY_S
        if first:
            self.on_alert("ERROR", f"{seg} positions unavailable ({err}). {seg} trades are NOT protected "
                                   f"until this works. Retrying every {SEGMENT_RETRY_S // 60} min.")

    def _segment_active(self, seg: str) -> bool:
        return self._seg_down.get(seg, 0) <= time.monotonic()

    def _positions_seg(self, seg):
        if not self._segment_active(seg):
            return []
        try:
            resp = self._call("nontrading", "get_positions_for_user", segment=seg) or {}
        except RequestRejected as e:
            if seg in self.CORE_SEGMENTS:
                raise
            self._segment_unavailable(seg, e)
            return []
        if seg in self._seg_down:
            del self._seg_down[seg]
            self.on_alert("WARN", f"{seg} positions available again - protected.")
        rows = resp.get("positions", []) or []
        for p in rows:
            p.setdefault("segment", seg)
        return rows

    def positions(self) -> list[dict]:
        out = []
        for rows in self._pool.map(self._positions_seg, list(self.segments)):  # parallel per segment
            out.extend(rows)
        return out

    def ltp(self, positions: list[dict]) -> dict:
        """Batched LTP. A failed batch is skipped (engine reuses the last price and warns when
        it gets stale); only if EVERY batch fails do we raise."""
        by_seg: dict[str, list[str]] = {}
        for p in positions:
            if int(p.get("quantity") or 0):
                by_seg.setdefault(p["segment"], []).append(f'{p["exchange"]}_{p["trading_symbol"]}')
        jobs = []
        for seg, syms in by_seg.items():
            for i in range(0, len(syms), 50):
                jobs.append((seg, tuple(syms[i:i + 50])))

        def fetch(j):
            try:
                return self._call("live", "get_ltp", segment=j[0], exchange_trading_symbols=j[1]) or {}
            except BrokerError as e:
                self.ltp_errors += 1
                log.warning("LTP batch failed (%s): %s", j[0], e)
                return None

        prices, failed = {}, 0
        for resp in self._pool.map(fetch, jobs):
            if resp is None:
                failed += 1
                continue
            prices.update({k: float(v) for k, v in resp.items() if v is not None})
        if jobs and failed == len(jobs):
            raise BrokerError("all LTP requests failed")
        return prices

    def _orders_seg(self, seg):
        if not self._segment_active(seg):
            return []
        out, page = [], 0
        while page < 40:
            try:
                resp = self._call("nontrading", "get_order_list", segment=seg, page=page, page_size=25) or {}
            except RequestRejected as e:
                if seg in self.CORE_SEGMENTS:
                    raise
                self._segment_unavailable(seg, e)
                return out
            batch = resp.get("order_list", []) or []
            for o in batch:
                o.setdefault("segment", seg)
            out.extend(batch)
            if len(batch) < 25:
                break
            page += 1
        return out

    def orders_today(self) -> list[dict]:
        out = []
        for rows in self._pool.map(self._orders_seg, list(self.segments)):
            out.extend(rows)
        return out

    # ---------------- writes (priority budget)
    def place_order(self, **kw) -> dict:
        return self._call("orders", "place_order", priority=True, **kw) or {}

    def cancel_order(self, groww_order_id: str, segment: str) -> dict:
        return self._call("orders", "cancel_order", priority=True,
                          groww_order_id=groww_order_id, segment=segment) or {}


class MockBroker:
    """Simulated Groww account with *asynchronous* fills, rejections and outages,
    so the executor's no-double-exit logic is actually exercised."""

    UNIVERSE = [("NIFTY25NOV24000CE", "FNO", "NSE", "NRML", 75, 120.0),
                ("BANKNIFTY25NOV52000PE", "FNO", "NSE", "NRML", 35, 300.0),
                ("RELIANCE", "CASH", "NSE", "MIS", 50, 1300.0),
                ("TATASTEEL", "CASH", "NSE", "CNC", 200, 150.0),
                ("GOLDM25DECFUT", "COMMODITY", "MCX", "NRML", 1, 7600.0)]

    def __init__(self, drift=-0.003, vol=0.008, trade_prob=0.3, fill_delay_steps=1,
                 reject_prob=0.0, outage_prob=0.0, seed=None):
        self.rng = random.Random(seed)
        self.drift, self.vol, self.trade_prob = drift, vol, trade_prob
        self.fill_delay, self.reject_prob, self.outage_prob = fill_delay_steps, reject_prob, outage_prob
        self.book, self.px, self.realised = {}, {}, {}
        self.orders: list[dict] = []
        self.n = 0
        self.user_trading = True

    def _fill(self, o):
        p = self.book[o["trading_symbol"]]
        sign = 1 if o["transaction_type"] == "BUY" else -1
        q = o["quantity"] * sign
        px = self.px[o["trading_symbol"]]
        if p["quantity"] == 0 or (p["quantity"] > 0) == (q > 0):  # open / add
            tot = p["quantity"] + q
            p["net_price"] = (p["net_price"] * abs(p["quantity"]) + px * abs(q)) / abs(tot)
            p["quantity"] = tot
        else:  # reduce (never flips in the mock unless asked to)
            closed = min(abs(q), abs(p["quantity"]))
            s = 1 if p["quantity"] > 0 else -1
            self.realised[o["trading_symbol"]] = self.realised.get(o["trading_symbol"], 0) + (px - p["net_price"]) * closed * s
            p["quantity"] += q
            if abs(q) > closed:  # flipped
                p["net_price"] = px
        side_field = "credit_quantity" if o["transaction_type"] == "BUY" else "debit_quantity"
        p[side_field] = p.get(side_field, 0) + o["quantity"]
        o["order_status"], o["filled_quantity"] = "EXECUTED", o["quantity"]

    def tick(self):
        """One simulated time step: prices move, pending orders fill, user maybe trades."""
        self.n += 1
        for s in self.px:
            self.px[s] *= 1 + self.rng.gauss(self.drift, self.vol)
        for o in self.orders:
            if o["order_status"] == "OPEN" and self.n - o["_t"] >= self.fill_delay:
                self._fill(o)
        if self.user_trading and self.rng.random() < self.trade_prob:
            sym, seg, exch, prod, lot, px0 = self.rng.choice(self.UNIVERSE)
            self.px.setdefault(sym, px0)
            self.book.setdefault(sym, dict(trading_symbol=sym, segment=seg, exchange=exch, product=prod,
                                           quantity=0, net_price=0.0, net_carry_forward_quantity=0,
                                           credit_quantity=0, debit_quantity=0))
            o = self._new_order(sym, lot, "BUY", "")
            self._fill(o)  # user's market order fills instantly

    def _maybe_outage(self):
        if self.rng.random() < self.outage_prob:
            raise BrokerError("simulated network outage")

    def _new_order(self, sym, qty, side, ref):
        o = {"groww_order_id": f"G{len(self.orders):06d}", "trading_symbol": sym, "quantity": qty,
             "transaction_type": side, "order_status": "OPEN", "order_reference_id": ref,
             "segment": self.book[sym]["segment"], "filled_quantity": 0, "_t": self.n}
        self.orders.append(o)
        return o

    def positions(self):
        self._maybe_outage()
        return [dict(p, realised_pnl=round(self.realised.get(s, 0.0), 2)) for s, p in self.book.items()]

    def ltp(self, positions):
        return {f'{p["exchange"]}_{p["trading_symbol"]}': self.px[p["trading_symbol"]]
                for p in positions if int(p.get("quantity") or 0)}

    def orders_today(self):
        self._maybe_outage()
        return [dict(o) for o in self.orders]

    def place_order(self, **kw):
        self._maybe_outage()
        sym = kw["trading_symbol"]
        o = self._new_order(sym, int(kw["quantity"]), kw["transaction_type"], kw.get("order_reference_id", ""))
        if self.rng.random() < self.reject_prob:
            o["order_status"] = "REJECTED"
        return {"groww_order_id": o["groww_order_id"], "order_status": o["order_status"],
                "order_reference_id": o["order_reference_id"]}

    def cancel_order(self, groww_order_id, segment):
        for o in self.orders:
            if o["groww_order_id"] == groww_order_id and o["order_status"] == "OPEN":
                o["order_status"] = "CANCELLED"
        return {"groww_order_id": groww_order_id, "order_status": "CANCELLED"}

    def is_agent_order(self, o):
        return str(o.get("order_reference_id", "")).startswith(AGENT_REF_PREFIX)
