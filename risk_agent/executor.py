"""Safe exit execution.

Guarantees:
  * The agent only ever REDUCES exposure. Exit qty is capped at the live position size
    minus anything already in flight, so a slow fill can never be double-sold into a
    reverse position.
  * Orders above the exchange freeze quantity are sliced.
  * In-flight orders are tracked by our own order_reference_id and reconciled against
    the order book; stuck orders are cancelled and re-sent; after 2 rejections the
    next attempt uses a marketable LIMIT 2% through LTP.
  * Outside market hours nothing is sent (it would be rejected / become an AMO).
  * Every order and response goes to an append-only audit log.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

from .config import (AGENT_REF_PREFIX, AUDIT_DIR, DEFAULT_SLICE_QTY, EXIT_ORDER_TIMEOUT_S,
                     FREEZE_QTY, market_open, now_ist)
from .errors import RequestRejected

log = logging.getLogger("executor")
TERMINAL = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED", "REJECTED", "FAILED", "CANCELLED"}
FILLED = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"}
FILL_GRACE_S = 30      # forget filled exits after this long
RETRY_BACKOFF = float(os.environ.get("RISK_AGENT_RETRY_BACKOFF", "1"))
FILL_FALLBACK_S = 3    # if the position has no per-side counters, assume it caught up after this
UNKNOWN_DROP_S = 10    # an order whose send errored (outcome unknown) is forgotten after this
NEVER_SEEN_DROP_S = 60


def side_cum(p: dict, side: str):
    """Cumulative quantity traded today on one side (Groww: credit = bought, debit = sold)."""
    v = p.get("debit_quantity" if side == "SELL" else "credit_quantity")
    return None if v is None else int(v)


_seq = 0


def new_ref() -> str:
    """8-20 alphanumeric chars as Groww requires; unique per process."""
    global _seq
    _seq = (_seq + 1) % 1000
    return f"{AGENT_REF_PREFIX}{int(time.time() * 10) % 10**9:09d}{_seq:03d}"  # 7+9+3 = 19


def tick_round(px: float, tick: float = 0.05) -> float:
    return round(round(px / tick) * tick, 2)


def underlying_of(symbol: str) -> str:
    for u in sorted(FREEZE_QTY, key=len, reverse=True):
        if symbol.startswith(u):
            return u
    return ""


def slice_qty(symbol: str, qty: int) -> list[int]:
    cap = FREEZE_QTY.get(underlying_of(symbol), DEFAULT_SLICE_QTY) - 1  # strictly below freeze
    cap = max(1, cap)
    out = []
    while qty > 0:
        out.append(min(cap, qty))
        qty -= out[-1]
    return out


def pos_key(p) -> str:
    return f'{p["segment"]}:{p["trading_symbol"]}:{p["product"]}'


@dataclass
class InFlight:
    ref: str
    key: str
    qty: int
    side: str
    segment: str
    sent_at: float
    cum_at_send: int | None = None
    groww_order_id: str | None = None
    status: str = "SENT"
    filled_at: float | None = None


@dataclass
class ExitExecutor:
    broker: object
    alert: callable = lambda level, msg: None
    inflight: dict = field(default_factory=dict)       # ref -> InFlight
    rejections: dict = field(default_factory=dict)     # key -> count today
    escalated: set = field(default_factory=set)
    waiting_logged: set = field(default_factory=set)
    last_reject_at: dict = field(default_factory=dict)  # key -> time of last rejection
    cancel_sent: set = field(default_factory=set)      # user order ids we already asked to cancel

    def new_day(self):
        self.rejections.clear()
        self.escalated.clear()
        self.waiting_logged.clear()
        self.last_reject_at.clear()
        self.cancel_sent.clear()

    # ---------------- bookkeeping
    def reconcile(self, orders: list[dict]):
        by_ref = {o.get("order_reference_id"): o for o in orders if o.get("order_reference_id")}
        by_id = {o.get("groww_order_id"): o for o in orders}
        now = time.time()
        for ref, f in list(self.inflight.items()):
            o = by_ref.get(ref) or (by_id.get(f.groww_order_id) if f.groww_order_id else None)
            if o:
                f.groww_order_id = o.get("groww_order_id") or f.groww_order_id
                f.status = str(o.get("order_status", "")).upper()
                if f.status in FILLED:
                    f.filled_at = f.filled_at or now
                    if now - f.filled_at > FILL_GRACE_S:
                        self.inflight.pop(ref)
                    continue
                if f.status in TERMINAL:
                    if f.status not in FILLED:
                        self.rejections[f.key] = self.rejections.get(f.key, 0) + 1
                        self.last_reject_at[f.key] = now
                        log.warning("exit %s %s: %s", f.key, ref, f.status)
                    self.inflight.pop(ref)
                elif now - f.sent_at > EXIT_ORDER_TIMEOUT_S and f.groww_order_id and f.status != "CANCELLATION_REQUESTED":
                    log.warning("exit %s stuck (%s) - cancelling to re-send", ref, f.status)
                    try:
                        self.broker.cancel_order(groww_order_id=f.groww_order_id, segment=f.segment)
                        f.status = "CANCELLATION_REQUESTED"
                    except Exception as e:
                        log.error("cancel of stuck exit failed: %s", e)
            elif now - f.sent_at > (UNKNOWN_DROP_S if f.status == "UNKNOWN" else NEVER_SEEN_DROP_S):
                log.error("exit %s never appeared in order book; dropping", ref)
                self.inflight.pop(ref)

    def pending_qty(self, p: dict) -> int:
        """Qty already on its way out. A FILLED exit still counts until the positions
        feed reflects it (that side's cumulative traded qty has grown by our qty),
        so a lagging positions API can't make us sell the same lots twice."""
        key, n, now = pos_key(p), 0, time.time()
        for f in self.inflight.values():
            if f.key != key:
                continue
            if f.status not in FILLED:
                n += f.qty
                continue
            cum = side_cum(p, f.side)
            if f.cum_at_send is not None and cum is not None:
                reflected = cum >= f.cum_at_send + f.qty
            else:
                reflected = now - (f.filled_at or now) > FILL_FALLBACK_S
            if not reflected:
                n += f.qty
        return n

    def audit(self, event: dict):
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        event = {"ts": now_ist().isoformat(), **event}
        with open(AUDIT_DIR / f"{now_ist():%Y-%m-%d}.jsonl", "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    # ---------------- actions
    def exit(self, p: dict, want_qty: int, reason: str, ltp: float | None, mode: str) -> int:
        """Reduce position p by up to want_qty. Returns qty sent."""
        key = pos_key(p)
        live = int(p.get("quantity") or 0)
        if live == 0:
            return 0
        qty = min(abs(want_qty), abs(live) - self.pending_qty(p))
        if qty <= 0:
            return 0
        if mode != "enforce":
            self.audit({"type": "would_exit", "key": key, "qty": qty, "reason": reason})
            return 0
        if not market_open(p["segment"]):
            if key not in self.waiting_logged:
                self.waiting_logged.add(key)
                self.alert("WARN", f"{p['trading_symbol']}: market closed - will exit at next open ({reason})")
            return 0
        self.waiting_logged.discard(key)
        n_rej = self.rejections.get(key, 0)
        if n_rej and time.time() - self.last_reject_at.get(key, 0) < RETRY_BACKOFF * min(2 ** (n_rej - 1), 15):
            return 0  # back off 1, 2, 4, 8, 15 s between retries after rejections
        side = "SELL" if live > 0 else "BUY"
        use_limit = self.rejections.get(key, 0) >= 2 and ltp
        if self.rejections.get(key, 0) >= 5 and key not in self.escalated:
            self.escalated.add(key)
            self.alert("CRITICAL", f"Exits for {p['trading_symbol']} rejected 5+ times. "
                                   "CLOSE IT MANUALLY IN THE GROWW APP NOW.")
        sent = 0
        for q in slice_qty(p["trading_symbol"], qty):
            ref = new_ref()
            kw = dict(trading_symbol=p["trading_symbol"], quantity=q, validity="DAY",
                      exchange=p["exchange"], segment=p["segment"], product=p["product"],
                      transaction_type=side, order_type="MARKET", order_reference_id=ref)
            if use_limit:
                kw.update(order_type="LIMIT", price=tick_round(ltp * (0.98 if side == "SELL" else 1.02)))
            self.inflight[ref] = InFlight(ref, key, q, side, p["segment"], time.time(),
                                          cum_at_send=None if side_cum(p, side) is None else side_cum(p, side) + sent)
            t0 = time.perf_counter()
            try:
                resp = self.broker.place_order(**kw)
                ms = (time.perf_counter() - t0) * 1000
                f = self.inflight[ref]
                f.groww_order_id = resp.get("groww_order_id")
                f.status = str(resp.get("order_status", "SENT")).upper()
                self.audit({"type": "exit_sent", "reason": reason, "req": kw, "resp": resp, "latency_ms": round(ms)})
                log.info("EXIT %s %s x%d (%s) %.0fms -> %s", side, p["trading_symbol"], q, reason, ms, f.status)
                sent += q
            except RequestRejected as e:
                # broker definitively refused: nothing is in flight, retry per the backoff schedule
                self.audit({"type": "exit_refused", "reason": reason, "req": kw, "error": str(e)})
                log.error("exit refused %s: %s", p["trading_symbol"], e)
                self.inflight.pop(ref, None)
                self.rejections[key] = self.rejections.get(key, 0) + 1
                self.last_reject_at[key] = time.time()
                break
            except Exception as e:
                # unknown outcome: keep it in-flight; reconcile() finds it by ref or drops it after UNKNOWN_DROP_S
                self.audit({"type": "exit_error", "reason": reason, "req": kw, "error": str(e)})
                log.error("exit order error %s: %s", p["trading_symbol"], e)
                self.inflight[ref].status = "UNKNOWN"
                break
        return sent

    def cancel_all_open(self, orders: list[dict]) -> int:
        """Cancel the user's pending orders (not ours). Called every tick while LOCKED."""
        n = 0
        for o in orders:
            st = str(o.get("order_status", "")).upper()
            oid = o.get("groww_order_id")
            if st in TERMINAL or st == "CANCELLATION_REQUESTED" or oid in self.cancel_sent:
                continue
            if str(o.get("order_reference_id") or "").startswith(AGENT_REF_PREFIX):
                continue
            try:
                self.broker.cancel_order(groww_order_id=oid, segment=o["segment"])
                self.cancel_sent.add(oid)
                self.audit({"type": "cancel_user_order", "order": o})
                n += 1
            except Exception as e:
                log.error("cancel %s failed: %s", oid, e)
        return n
