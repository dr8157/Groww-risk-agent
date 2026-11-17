"""Risk engine: deterministic rules, frozen-at-09:00 config, persistent day state."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .config import (ACTIVE_PATH, AGENT_REF_PREFIX, CONFIG_PATH, HISTORY_PATH, IDLE_POLL_SECONDS, IST,
                     ORDERS_POLL_SECONDS, POLL_SECONDS, STALE_DATA_SECONDS, STALE_PRICE_SECONDS,
                     STATE_PATH, any_market_open, file_mtime_ist, in_edit_window, now_ist, read_json,
                     sanitize, write_json)
from .executor import ExitExecutor, pos_key

log = logging.getLogger("engine")
EXECUTED = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"}


@dataclass
class DayState:
    date: str
    rules: dict
    status: str = "ACTIVE"  # ACTIVE | ORDER_CAP | COOLDOWN | LOCKED
    lock_reason: str = ""
    peak_pnl: float = 0.0
    user_orders: int = 0
    consecutive_losses: int = 0
    caution_triggered: bool = False
    cooldown_until: str | None = None
    cap_warned: bool = False
    prev_qty: dict = field(default_factory=dict)
    prev_realised: dict = field(default_factory=dict)
    rejected_saves: list = field(default_factory=list)
    events: list = field(default_factory=list)


class RiskEngine:
    def __init__(self, broker, notifier=None, mock=False):
        self.broker = broker
        self.notifier = notifier
        self.mock = mock
        self.executor = ExitExecutor(broker, alert=self.alert)
        if hasattr(broker, "on_alert"):
            broker.on_alert = self.alert
        if not ACTIVE_PATH.exists():
            write_json(ACTIVE_PATH, sanitize({}))
        self.day = self._load_or_new_day()
        self.last_ltp: dict[str, tuple[float, float]] = {}
        self.orders: list[dict] = []
        self.last_orders_at = 0.0
        self.last_ok = time.time()
        self.blind_alerted = False
        self.errors = 0
        self.loop_ms = 0.0
        self.has_open_positions = False
        self._would_logged: set = set()

    # ---------------- logging / alerts
    def log(self, level, msg):
        ev = {"t": now_ist().strftime("%H:%M:%S"), "level": level, "msg": msg}
        self.day.events = (self.day.events + [ev])[-300:]
        getattr(log, {"ERROR": "error", "CRITICAL": "critical", "WARN": "warning"}.get(level, "info"))(msg)

    def alert(self, level, msg):
        if self.day.events and self.day.events[-1]["msg"] == msg:
            return  # same condition still true - don't flood the log
        self.log(level, msg)
        if self.notifier and level in ("LOCK", "WARN", "CRITICAL", "ERROR"):
            self.notifier.send(level, msg)

    # ---------------- persistence
    def _load_or_new_day(self) -> DayState:
        today = now_ist().date().isoformat()
        st = read_json(STATE_PATH, {})
        if st.get("date") == today and "day" in st:
            d = st["day"]
            return DayState(**{k: d[k] for k in DayState.__dataclass_fields__ if k in d})
        if st.get("date") and st.get("date") != today:
            # state.json still holds the last REAL tick of the finished day: archive that.
            hist = read_json(HISTORY_PATH, {})
            hist[st["date"]] = st.get("day_pnl", 0)
            write_json(HISTORY_PATH, hist)
        return DayState(date=today, rules=sanitize(read_json(ACTIVE_PATH, {})))

    def weekly_before_today(self) -> float:
        hist = read_json(HISTORY_PATH, {})
        today = now_ist().date()
        monday = (today - timedelta(days=today.weekday())).isoformat()
        return sum(float(v) for k, v in hist.items() if monday <= k < today.isoformat())

    # ---------------- change control
    def sync_rules(self):
        req = read_json(CONFIG_PATH, None)
        active = sanitize(read_json(ACTIVE_PATH, {}))
        if req and req.get("saved_at") and req["saved_at"] != active.get("saved_at"):
            try:
                saved = datetime.fromisoformat(req["saved_at"]).astimezone(IST)
            except ValueError:
                saved = None
            # The claimed saved_at can be forged by hand-editing the file, so the file's real
            # modification time must ALSO be inside the 00:00-09:00 window.
            mtime = file_mtime_ist(CONFIG_PATH)
            ok = (saved and in_edit_window(saved) and (mtime is None or in_edit_window(mtime))
                  and saved <= now_ist() + timedelta(minutes=1))
            if ok:
                active = sanitize(req)
                write_json(ACTIVE_PATH, active)
                self.day.rules = active
                self.alert("WARN", f"Rule change saved {saved:%d %b %H:%M} accepted: "
                                   f"daily ₹{active['daily_loss_limit']:,.0f}, weekly ₹{active['weekly_loss_limit']:,.0f}, "
                                   f"orders {active['max_orders_per_day']}, mode {active['mode']}")
            elif req["saved_at"] not in self.day.rejected_saves:
                self.day.rejected_saves.append(req["saved_at"])
                when = f"{saved:%d %b %H:%M}" if saved else "invalid time"
                self.alert("WARN", f"REJECTED rule change saved {when}: changes only allowed before 09:00 IST.")
        if in_edit_window():
            self.day.rules = active

    # ---------------- helpers
    def exitable(self, p, rules) -> int:
        q = int(p.get("quantity") or 0)
        if p.get("product") == "CNC":
            if not rules["include_delivery"] or q <= 0:
                return 0
            return max(0, q - int(p.get("net_carry_forward_quantity") or 0))
        return abs(q)

    def do_exit(self, p, qty, reason, rules):
        q = min(qty, self.exitable(p, rules))
        if q > 0:
            ltp = self.last_ltp.get(f'{p["exchange"]}_{p["trading_symbol"]}', (None, 0))[0]
            sent = self.executor.exit(p, q, reason, ltp, rules["mode"])
            if sent:
                self.log("EXIT", f"{p['trading_symbol']} x{sent} - {reason}")
            elif rules["mode"] != "enforce" and (p["trading_symbol"], reason) not in self._would_logged:
                self._would_logged.add((p["trading_symbol"], reason))
                self.log("ALERT", f"[alert-only] would exit {p['trading_symbol']} x{q}: {reason}")

    def count_user_orders(self, orders) -> int:
        today = self.day.date
        n = 0
        for o in orders:
            if str(o.get("order_status", "")).upper() not in EXECUTED:
                continue
            if str(o.get("order_reference_id") or "").startswith(AGENT_REF_PREFIX):
                continue
            created = str(o.get("created_at") or o.get("trade_date") or "")
            if created[:10] and created[:10] != today and created[:4].isdigit():
                continue
            n += 1
        return n

    def refresh_orders(self, force=False):
        due = time.time() - self.last_orders_at >= ORDERS_POLL_SECONDS
        if force or due or self.executor.inflight or self.day.status == "LOCKED":
            self.orders = self.broker.orders_today()
            self.last_orders_at = time.time()
            self.executor.reconcile(self.orders)
            self.day.user_orders = self.count_user_orders(self.orders)

    def next_delay(self) -> float:
        """Poll fast while markets are open (or anything is exposed / in flight); slowly otherwise."""
        if self.mock or any_market_open() or self.has_open_positions or self.executor.inflight:
            return POLL_SECONDS
        return IDLE_POLL_SECONDS

    # ---------------- main step
    def step(self):
        t0 = time.perf_counter()
        if now_ist().date().isoformat() != self.day.date:
            # state.json still has the finished day's last real P&L; _load_or_new_day archives it
            self.day = self._load_or_new_day()
            self._would_logged.clear()
            self.executor.new_day()
            self.log("INFO", "new day - counters reset")
        maintain = getattr(self.broker, "maintain", None)
        if maintain:
            maintain()  # proactive daily token refresh
        self.sync_rules()
        rules = self.day.rules

        try:
            positions = self.broker.positions()
            ltps = self.broker.ltp(positions)
            self.refresh_orders()
        except Exception as e:
            self.errors += 1
            blind_for = time.time() - self.last_ok
            self.log("ERROR", f"broker read failed ({type(e).__name__}: {e}); blind for {blind_for:.0f}s")
            if blind_for > STALE_DATA_SECONDS and not self.blind_alerted:
                self.blind_alerted = True
                self.alert("CRITICAL", f"Risk agent can't reach Groww for {blind_for:.0f}s - "
                                       "you are NOT protected. Check positions in the app.")
            if self.notifier:
                self.notifier.heartbeat(ok=False)
            self.save_health()
            raise
        if self.blind_alerted:
            self.alert("WARN", "Connection to Groww restored - protection active again.")
        self.blind_alerted, self.errors, self.last_ok = False, 0, time.time()
        now = time.time()
        for k, v in ltps.items():
            self.last_ltp[k] = (v, now)

        # ----- mark to market
        realised = unrealised = 0.0
        rows, increased, missing_px, stale_px = [], [], [], []
        for p in positions:
            k, q = pos_key(p), int(p.get("quantity") or 0)
            r = float(p.get("realised_pnl") or 0)
            avg = float(p.get("net_price") or 0)
            px, px_t = self.last_ltp.get(f'{p["exchange"]}_{p["trading_symbol"]}', (None, 0))
            if q and px is None:
                missing_px.append(p["trading_symbol"])
            elif q and now - px_t > STALE_PRICE_SECONDS:
                stale_px.append(p["trading_symbol"])
            u = (px - avg) * q if q and px is not None else 0.0
            realised += r
            unrealised += u
            rows.append(dict(symbol=p["trading_symbol"], segment=p["segment"], product=p["product"], qty=q,
                             avg=round(avg, 2), ltp=px, ltp_age_s=round(now - px_t, 1) if px else None,
                             unrealised=round(u, 2), realised=round(r, 2),
                             exiting=self.executor.pending_qty(p)))
            pq = self.day.prev_qty.get(k, 0)
            flipped = pq and q and (q > 0) != (pq > 0)
            added = abs(q) if (pq == 0 or flipped) else max(0, abs(q) - abs(pq))
            if q and added:
                increased.append((p, added))
            if pq and not q:
                delta = r - self.day.prev_realised.get(k, 0)
                self.day.consecutive_losses = self.day.consecutive_losses + 1 if delta < 0 else 0
            self.day.prev_qty[k], self.day.prev_realised[k] = q, r
        self.has_open_positions = any(r["qty"] for r in rows)
        if missing_px:
            self.alert("ERROR", f"No price for {', '.join(missing_px)} - open P&L incomplete. "
                                "Is the paid Trade API plan active?")
        if stale_px:
            self.alert("WARN", f"Price for {', '.join(stale_px)} is over {STALE_PRICE_SECONDS:.0f}s old - "
                               "open P&L may be wrong.")
        pnl = realised + unrealised
        self.day.peak_pnl = max(self.day.peak_pnl, pnl)
        week = self.weekly_before_today() + pnl

        # ----- 1. hard locks
        reason = None
        if pnl <= -rules["daily_loss_limit"]:
            reason = f"daily loss ₹{-pnl:,.0f} hit limit ₹{rules['daily_loss_limit']:,.0f}"
        elif rules["weekly_loss_limit"] and week <= -rules["weekly_loss_limit"]:
            reason = f"weekly loss ₹{-week:,.0f} hit limit ₹{rules['weekly_loss_limit']:,.0f}"
        elif (rules["profit_protection_enabled"] and self.day.peak_pnl >= rules["profit_giveback_trigger"]
              and pnl <= self.day.peak_pnl * (1 - rules["profit_giveback_pct"] / 100)):
            reason = f"profit protection: peak ₹{self.day.peak_pnl:,.0f}, now ₹{pnl:,.0f}"
        if reason and self.day.status != "LOCKED":
            self.day.status, self.day.lock_reason = "LOCKED", reason
            self.alert("LOCK", f"LOCKED for the day - {reason}. Squaring off everything.")
            if rules["mode"] == "enforce":
                self.refresh_orders(force=True)
        if self.day.status == "LOCKED":
            if rules["mode"] == "enforce":
                n = self.executor.cancel_all_open(self.orders)  # every tick: new pending orders die too
                if n:
                    self.log("INFO", f"cancelled {n} pending orders")
            for p in positions:
                if int(p.get("quantity") or 0):
                    self.do_exit(p, abs(int(p["quantity"])), "locked", rules)
            return self.finish(t0, pnl, realised, unrealised, rows, week)

        # ----- 2. order cap
        cap = int(rules["max_orders_per_day"] or 0)
        if cap and self.day.user_orders > cap:
            if self.day.status != "ORDER_CAP":
                self.day.status = "ORDER_CAP"
                self.alert("LOCK", f"Order cap: {self.day.user_orders} orders > {cap}. "
                                   "New positions / added size will be reversed. Closing is allowed.")
            for p, added in increased:
                self.do_exit(p, added, f"order cap {cap}", rules)
        elif cap and self.day.user_orders == cap and not self.day.cap_warned:
            self.day.cap_warned = True
            self.alert("WARN", f"{cap}/{cap} orders used - your next new order will be reversed.")

        # ----- 3. optional anti-revenge cool-down
        if rules["anti_revenge_enabled"] and self.day.status != "ORDER_CAP":
            why = None
            if not self.day.caution_triggered and pnl <= -rules["daily_loss_limit"] * rules["caution_level_pct"] / 100:
                self.day.caution_triggered, why = True, f"down ₹{-pnl:,.0f}"
            elif self.day.consecutive_losses >= rules["consecutive_losses_for_cooldown"]:
                self.day.consecutive_losses, why = 0, f"{int(rules['consecutive_losses_for_cooldown'])} losses in a row"
            if why:
                until = now_ist() + timedelta(minutes=rules["cooldown_minutes"])
                self.day.cooldown_until, self.day.status = until.isoformat(), "COOLDOWN"
                self.alert("WARN", f"Cool-down until {until:%H:%M} ({why}). New trades will be closed.")
            if self.day.cooldown_until and now_ist() < datetime.fromisoformat(self.day.cooldown_until):
                for p, added in increased:
                    self.do_exit(p, added, "cool-down", rules)
            elif self.day.status == "COOLDOWN":
                self.day.status = "ACTIVE"
                self.log("INFO", "cool-down over")
        return self.finish(t0, pnl, realised, unrealised, rows, week)

    # ---------------- output
    def finish(self, t0, pnl, realised, unrealised, rows, week):
        self.loop_ms = (time.perf_counter() - t0) * 1000
        self.save(pnl, realised, unrealised, rows, week)
        if self.notifier:
            self.notifier.heartbeat(ok=True)

    def health(self):
        h = {"last_ok": datetime.fromtimestamp(self.last_ok, IST).isoformat(), "consecutive_errors": self.errors,
             "loop_ms": round(self.loop_ms), "inflight_exits": len(self.executor.inflight)}
        lim = getattr(self.broker, "limits", None)
        if lim:
            h["rate"] = lim.usage()
        auth = getattr(self.broker, "auth_status", None)
        if auth:
            h["auth"] = auth()
        return h

    def save_health(self):
        st = read_json(STATE_PATH, {})
        if st:
            st["updated"] = now_ist().isoformat()
            st["health"] = self.health()
            st["day"] = asdict(self.day)
            write_json(STATE_PATH, st)

    def save(self, pnl, realised, unrealised, rows, week):
        write_json(STATE_PATH, {
            "date": self.day.date, "updated": now_ist().isoformat(), "mock": self.mock,
            "status": self.day.status, "lock_reason": self.day.lock_reason,
            "day_pnl": round(pnl, 2), "realised": round(realised, 2), "unrealised": round(unrealised, 2),
            "peak_pnl": round(self.day.peak_pnl, 2), "user_orders": self.day.user_orders,
            "weekly_pnl": round(week, 2), "rules": self.day.rules, "positions": rows,
            "health": self.health(), "day": asdict(self.day),
        })
