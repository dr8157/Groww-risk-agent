"""Entry point.

  python -m risk_agent.main --mock     simulated broker (safe)
  python -m risk_agent.main --check    live: log in, print what the agent sees, exit
  python -m risk_agent.main            live enforcement loop
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from .config import LOCK_PATH, LOG_DIR, POLL_SECONDS, ROOT

log = logging.getLogger("main")


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    from .config import IST
    fmt = logging.Formatter("%(asctime)s IST %(levelname)-7s %(name)-8s %(message)s")
    fmt.converter = lambda *_: datetime.now(IST).timetuple()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_DIR / "engine.log", maxBytes=5_000_000, backupCount=10)
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)


def single_instance():
    """Two engines would race each other's exits. Refuse to start a second one."""
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another risk agent is already running. Exiting.", file=sys.stderr)
        sys.exit(2)
    f.write(str(os.getpid()))
    f.flush()
    return f


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.environ.get("RISK_AGENT_ENV", ROOT / ".env"))
    except ImportError:
        pass


def check():
    from .brokers import GrowwBroker
    b = GrowwBroker(on_alert=lambda lvl, msg: print(f"  [{lvl}] {msg}"))
    mode = "TOTP (unattended)" if os.environ.get("GROWW_TOTP_SECRET") else "API-secret (needs DAILY approval)"
    print(f"auth flow: {mode}")
    if b.client is None:
        print(f"✗ login FAILED: {b.auth_error}")
        return 1
    print(f"✓ login OK  token status: {b.auth_status()}")
    t = time.perf_counter()
    pos = b.positions()
    print(f"✓ segments watched: {[s for s in b.segments if b._segment_active(s)]}  "
          f"segments down: {sorted(b._seg_down)}  positions call {1000 * (time.perf_counter() - t):.0f} ms")
    for p in pos:
        print(" ", {k: p.get(k) for k in ("trading_symbol", "segment", "product", "quantity", "net_price",
                                          "net_carry_forward_quantity", "realised_pnl",
                                          "credit_quantity", "debit_quantity")})
    try:
        t = time.perf_counter()
        px = b.ltp(pos)
        print(f"✓ LTP OK ({1000 * (time.perf_counter() - t):.0f} ms): {px or '(no open positions to price)'}")
    except Exception as e:
        print(f"✗ LTP FAILED - paid Trade API plan probably not active: {e}")
    orders = b.orders_today()
    print(f"✓ orders today: {len(orders)}")
    for o in orders[:5]:
        print(" ", {k: o.get(k) for k in ("trading_symbol", "order_status", "transaction_type", "quantity",
                                          "order_reference_id", "created_at")})
    print("\nOrder placement is NOT tested here (needs your static IP). See README -> 'Roll-out test'.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    load_env()
    if args.check:
        return check()

    setup_logging()
    _lock = single_instance()  # noqa: F841 (held for process lifetime)
    from .engine import RiskEngine
    from .notify import Notifier
    if args.mock:
        os.environ["RISK_AGENT_IGNORE_MARKET_HOURS"] = "1"
        from .brokers import MockBroker
        broker = MockBroker(seed=None)
    else:
        from .brokers import GrowwBroker
        broker = GrowwBroker()  # never raises on a failed login; retries with backoff and alerts
    notifier = Notifier()
    eng = RiskEngine(broker, notifier, mock=args.mock)
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    notifier.send("INFO", f"Risk agent started ({'MOCK' if args.mock else 'LIVE'}). "
                          f"Daily ₹{eng.day.rules['daily_loss_limit']:,.0f} · weekly ₹{eng.day.rules['weekly_loss_limit']:,.0f} "
                          f"· orders {eng.day.rules['max_orders_per_day']} · mode {eng.day.rules['mode']}")
    delay = POLL_SECONDS
    while not stop["flag"]:
        started = time.monotonic()
        if args.mock:
            broker.tick()
        try:
            eng.step()
            delay = eng.next_delay()
        except Exception as e:
            log.error("step failed: %s", e, exc_info=type(e).__name__ not in ("BrokerError", "AuthError", "RateLimited", "RequestRejected"))
            delay = min(max(delay, POLL_SECONDS) * 2, 10)  # back off on repeated failures, max 10 s
        time.sleep(max(0.0, delay - (time.monotonic() - started)))

    notifier.send("CRITICAL", "Risk agent STOPPED. You are not protected until it is restarted.")
    time.sleep(2)  # let the notifier flush


if __name__ == "__main__":
    sys.exit(main() or 0)
