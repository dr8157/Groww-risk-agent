import json

from risk_agent import config as C
from risk_agent.brokers import BrokerError, MockBroker
from risk_agent.engine import RiskEngine
from risk_agent.executor import slice_qty
from risk_agent.ratelimit import SlidingWindowLimiter


def save_cfg(at, **kw):
    c = C.sanitize({})
    c.update(kw)
    c["saved_at"] = at + "+05:30"
    C.write_json(C.CONFIG_PATH, c)


def run(eng, broker, n):
    for _ in range(n):
        broker.tick()
        try:
            eng.step()
        except BrokerError:
            pass


def state():
    return json.loads(C.STATE_PATH.read_text())


# ---------------------------------------------------------------- change window
def test_save_before_nine_is_accepted(monkeypatch):
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-10T08:30")
    save_cfg("2026-11-10T08:30", daily_loss_limit=8000)
    b = MockBroker(trade_prob=0, seed=1)
    e = RiskEngine(b)
    e.step()
    assert e.day.rules["daily_loss_limit"] == 8000


def test_save_after_nine_is_rejected_today_and_tomorrow(monkeypatch):
    b = MockBroker(trade_prob=0, seed=1)
    e = RiskEngine(b)
    save_cfg("2026-11-10T10:15", daily_loss_limit=50000, profit_protection_enabled=True)
    e.step()
    assert e.day.rules["daily_loss_limit"] == 10000
    assert not e.day.rules["profit_protection_enabled"]
    assert any("REJECTED" in ev["msg"] for ev in e.day.events)
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-11T08:00")
    e.step()
    assert e.day.rules["daily_loss_limit"] == 10000


def test_forged_saved_at_is_rejected_by_file_mtime(monkeypatch):
    """Hand-editing config.json at 10:00 and writing saved_at=08:00 must not work."""
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-10T10:00")
    monkeypatch.setenv("RISK_AGENT_FAKE_MTIME", "2026-11-10T10:00")  # real write time
    save_cfg("2026-11-10T08:00", daily_loss_limit=50000)              # forged claim
    e = RiskEngine(MockBroker(trade_prob=0, seed=1))
    e.step()
    assert e.day.rules["daily_loss_limit"] == 10000
    assert any("REJECTED" in ev["msg"] for ev in e.day.events)


def test_genuine_early_save_passes_mtime_check(monkeypatch):
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-10T08:30")
    monkeypatch.setenv("RISK_AGENT_FAKE_MTIME", "2026-11-10T08:29")
    save_cfg("2026-11-10T08:29", daily_loss_limit=8000)
    e = RiskEngine(MockBroker(trade_prob=0, seed=1))
    e.step()
    assert e.day.rules["daily_loss_limit"] == 8000


def test_rules_frozen_after_nine_even_if_active_file_edited(monkeypatch):
    b = MockBroker(trade_prob=0, seed=1)
    e = RiskEngine(b)
    e.step()
    C.write_json(C.ACTIVE_PATH, C.sanitize({"daily_loss_limit": 99999}))  # hand-edit the active file
    e.step()
    assert e.day.rules["daily_loss_limit"] == 10000


def test_hostile_config_is_clamped():
    c = C.sanitize({"daily_loss_limit": 0, "max_orders_per_day": "abc", "mode": "off"})
    assert c["daily_loss_limit"] == 1000 and c["mode"] == "enforce"


# ---------------------------------------------------------------- daily lock
def test_daily_loss_lock_and_reentry_squared_off():
    b = MockBroker(drift=-0.006, seed=3)
    e = RiskEngine(b)
    run(e, b, 150)
    s = state()
    assert s["status"] == "LOCKED" and "daily loss" in s["lock_reason"]
    lock_at = next(i for i, ev in enumerate(e.day.events) if ev["level"] == "LOCK")
    reentries = [ev for ev in e.day.events[lock_at:] if ev["level"] == "EXIT"]
    assert reentries, "positions opened after the lock must be exited"
    b.user_trading = False
    run(e, b, 5)
    assert all(p["qty"] == 0 for p in state()["positions"])


def test_lock_survives_restart():
    b = MockBroker(drift=-0.006, seed=3)
    e = RiskEngine(b)
    run(e, b, 150)
    e2 = RiskEngine(b)  # new process, same day
    assert e2.day.status == "LOCKED"


def test_pending_user_order_cancelled_after_lock():
    """An order the user places AFTER the lock (still unfilled) is cancelled, not left to fill."""
    b = MockBroker(drift=-0.006, seed=3, fill_delay_steps=50)
    e = RiskEngine(b)
    run(e, b, 150)
    assert state()["status"] == "LOCKED"
    sym = next(iter(b.book))
    o = b._new_order(sym, 10, "BUY", "")  # stays OPEN for 50 steps
    b.user_trading = False
    run(e, b, 3)
    assert o["order_status"] == "CANCELLED"


# ---------------------------------------------------------------- weekly lock + day rollover
def test_weekly_lock_carries_and_resets_monday(monkeypatch):
    C.write_json(C.HISTORY_PATH, {"2026-11-09": -12000, "2026-11-10": -15000})
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-11T10:00")
    b = MockBroker(drift=-0.004, seed=7)
    e = RiskEngine(b)
    run(e, b, 80)
    assert state()["status"] == "LOCKED" and "weekly" in state()["lock_reason"]
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-16T10:00")  # next Monday
    C.STATE_PATH.unlink()
    b2 = MockBroker(trade_prob=0, seed=1)
    e2 = RiskEngine(b2)
    e2.step()
    assert state()["status"] == "ACTIVE"


def test_midnight_rollover_archives_real_pnl_for_weekly_total(monkeypatch):
    """Regression: the engine used to overwrite the finished day's P&L with 0 at rollover."""
    save_cfg("2026-11-10T08:00", daily_loss_limit=1_000_000, weekly_loss_limit=0)
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-10T08:30")
    b = MockBroker(drift=-0.002, seed=4)
    e = RiskEngine(b)
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-10T14:00")
    run(e, b, 60)
    day1_pnl = state()["day_pnl"]
    assert day1_pnl < -100
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-11T10:00")  # same running process, next day
    e.step()
    hist = json.loads(C.HISTORY_PATH.read_text())
    assert abs(hist["2026-11-10"] - day1_pnl) < 1e-6
    assert e.weekly_before_today() == hist["2026-11-10"]


# ---------------------------------------------------------------- order cap
def test_order_cap_reverses_new_exposure_but_allows_holding():
    save_cfg("2026-11-10T08:00", daily_loss_limit=1_000_000, weekly_loss_limit=0)
    b = MockBroker(drift=0.0, vol=0.002, seed=11)
    e = RiskEngine(b)
    e.sync_rules()
    run(e, b, 200)
    s = state()
    assert s["status"] == "ORDER_CAP"
    cap_exits = [ev for ev in e.day.events if ev["level"] == "EXIT" and "order cap" in ev["msg"]]
    assert cap_exits
    assert s["user_orders"] > 30  # agent's own exits are not counted


# ---------------------------------------------------------------- execution safety
def test_never_oversells_with_slow_fills():
    """Fills take 3 steps; engine must not send a second exit for the same qty."""
    b = MockBroker(drift=-0.01, fill_delay_steps=3, seed=5)
    e = RiskEngine(b)
    run(e, b, 120)
    b.user_trading = False
    run(e, b, 20)
    for p in b.book.values():  # a position that flipped sign would mean we sold more than we held
        assert p["quantity"] >= 0, f"oversold {p['trading_symbol']}: {p['quantity']}"


class ListNotifier:
    def __init__(self):
        self.sent = []

    def send(self, level, msg, dedupe_s=60):
        self.sent.append((level, msg))

    def heartbeat(self, ok=True, every_s=60):
        pass


def test_rejections_escalate_to_limit_and_alert():
    b = MockBroker(drift=-0.01, reject_prob=1.0, seed=2)
    n = ListNotifier()
    e = RiskEngine(b, notifier=n)
    run(e, b, 120)
    assert any(lvl == "CRITICAL" and "MANUALLY" in m for lvl, m in n.sent)
    limit_orders = [o for o in b.orders if o.get("order_reference_id", "").startswith("RISKAGT")]
    assert len(limit_orders) > 2  # kept retrying


def test_outage_alerts_and_recovers(monkeypatch):
    monkeypatch.setattr(C, "STALE_DATA_SECONDS", 0)
    import risk_agent.engine as E
    monkeypatch.setattr(E, "STALE_DATA_SECONDS", 0)
    b = MockBroker(outage_prob=1.0, seed=1)
    e = RiskEngine(b)
    run(e, b, 3)
    assert any(ev["level"] == "CRITICAL" and "NOT protected" in ev["msg"] for ev in e.day.events)
    assert not any(o for o in b.orders if o.get("order_reference_id", "").startswith("RISKAGT"))
    b.outage_prob = 0
    run(e, b, 2)
    assert any("restored" in ev["msg"] for ev in e.day.events)


def test_alert_only_sends_nothing():
    save_cfg("2026-11-10T08:00", mode="alert_only")
    b = MockBroker(drift=-0.01, seed=3)
    e = RiskEngine(b)
    e.sync_rules()
    run(e, b, 100)
    assert not [o for o in b.orders if str(o["order_reference_id"]).startswith("RISKAGT")]


def test_old_delivery_holdings_never_sold():
    b = MockBroker(trade_prob=0, drift=-0.05, seed=1)
    b.px["TATASTEEL"] = 150.0
    b.book["TATASTEEL"] = dict(trading_symbol="TATASTEEL", segment="CASH", exchange="NSE", product="CNC",
                               quantity=1000, net_price=150.0, net_carry_forward_quantity=1000)
    e = RiskEngine(b)
    run(e, b, 30)
    assert state()["status"] == "LOCKED"
    assert b.book["TATASTEEL"]["quantity"] == 1000


def test_freeze_qty_slicing():
    assert slice_qty("NIFTY25NOV24000CE", 4000) == [1799, 1799, 402]
    assert slice_qty("BANKNIFTY25NOV52000PE", 900) == [899, 1]
    assert slice_qty("RELIANCE", 500) == [500]


def test_rate_limiter_caps_per_second():
    lim = SlidingWindowLimiter(10, 250)
    got = sum(lim.acquire(timeout=0) for _ in range(20))
    assert got == 8  # 80% share for non-priority
    assert lim.acquire(priority=True, timeout=0)  # exits can use the headroom


# ---------------------------------------------------------------- market hours / holidays
def test_mcx_open_late_evening_but_nse_closed(monkeypatch):
    from datetime import datetime
    monkeypatch.delenv("RISK_AGENT_IGNORE_MARKET_HOURS")
    t = datetime.fromisoformat("2026-11-10T23:40").replace(tzinfo=C.IST)
    assert C.market_open("COMMODITY", t) and not C.market_open("FNO", t)
    t2 = datetime.fromisoformat("2026-11-10T09:10").replace(tzinfo=C.IST)
    assert not C.market_open("CASH", t2) and C.market_open("COMMODITY", t2)


def test_holiday_calendar_blocks_segment(monkeypatch):
    from datetime import datetime
    monkeypatch.delenv("RISK_AGENT_IGNORE_MARKET_HOURS")
    C.write_json(C.HOLIDAYS_PATH, {"NSE": ["2026-11-10"]})
    t = datetime.fromisoformat("2026-11-10T11:00").replace(tzinfo=C.IST)
    assert not C.market_open("FNO", t) and C.market_open("COMMODITY", t)
