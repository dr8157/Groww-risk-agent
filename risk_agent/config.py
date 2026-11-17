"""Configuration, clock and change-window rules. Everything time-related is IST."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
EDIT_CUTOFF = dtime(9, 0)
TOKEN_RESET = dtime(6, 0)  # Groww access tokens expire at 06:00 IST every day

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RISK_AGENT_DATA", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"          # requested by the dashboard
ACTIVE_PATH = DATA_DIR / "active_config.json"   # accepted by the engine
STATE_PATH = DATA_DIR / "state.json"            # engine -> dashboard
HISTORY_PATH = DATA_DIR / "history.json"        # {date: day_pnl}
HOLIDAYS_PATH = DATA_DIR / "holidays.json"      # {"NSE": ["2026-11-10"], "MCX": [...]}
LOCK_PATH = DATA_DIR / "engine.lock"            # single-instance guard
AUDIT_DIR = DATA_DIR / "audit"
LOG_DIR = DATA_DIR / "logs"

AGENT_REF_PREFIX = "RISKAGT"

DEFAULT_CONFIG: dict = {
    "mode": "enforce",              # enforce | alert_only
    "daily_loss_limit": 10000,
    "weekly_loss_limit": 30000,     # 0 = off
    "max_orders_per_day": 30,       # 0 = off
    "include_delivery": True,
    # optional features, OFF
    "profit_protection_enabled": False,
    "profit_giveback_trigger": 15000,
    "profit_giveback_pct": 50,
    "anti_revenge_enabled": False,
    "caution_level_pct": 50,
    "cooldown_minutes": 20,
    "consecutive_losses_for_cooldown": 2,
    "saved_at": "2000-01-01T00:00:00+05:30",
}

# bounds the engine enforces even if config.json is hand-edited
BOUNDS = {
    "daily_loss_limit": (1000, 1_000_000),
    "weekly_loss_limit": (0, 5_000_000),
    "max_orders_per_day": (0, 500),
    "profit_giveback_trigger": (0, 1_000_000),
    "profit_giveback_pct": (10, 90),
    "caution_level_pct": (20, 90),
    "cooldown_minutes": (5, 240),
    "consecutive_losses_for_cooldown": (1, 20),
}

# engine tuning - not user rules, read from env so they can't be changed from the dashboard
POLL_SECONDS = float(os.environ.get("RISK_AGENT_POLL_SECONDS", "1.0"))
IDLE_POLL_SECONDS = float(os.environ.get("RISK_AGENT_IDLE_POLL_SECONDS", "30"))
ORDERS_POLL_SECONDS = float(os.environ.get("RISK_AGENT_ORDERS_POLL_SECONDS", "3.0"))
STALE_DATA_SECONDS = float(os.environ.get("RISK_AGENT_STALE_SECONDS", "20"))
STALE_PRICE_SECONDS = float(os.environ.get("RISK_AGENT_STALE_PRICE_SECONDS", "10"))
EXIT_ORDER_TIMEOUT_S = float(os.environ.get("RISK_AGENT_EXIT_TIMEOUT", "8"))

# Exchange freeze quantities (max qty per order). VERIFY against current NSE/BSE circulars;
# override with RISK_AGENT_FREEZE_QTY='{"NIFTY":1800,"BANKNIFTY":900}'.
FREEZE_QTY = {"NIFTY": 1800, "BANKNIFTY": 900, "FINNIFTY": 1800, "MIDCPNIFTY": 2800,
              "NIFTYNXT50": 600, "SENSEX": 1000, "BANKEX": 900}
FREEZE_QTY.update(json.loads(os.environ.get("RISK_AGENT_FREEZE_QTY", "{}") or "{}"))
DEFAULT_SLICE_QTY = int(os.environ.get("RISK_AGENT_DEFAULT_SLICE", "100000"))

# MCX evening close is 23:30 while US daylight saving applies (Mar-Nov) and 23:55 otherwise.
MCX_CLOSE = dtime.fromisoformat(os.environ.get("RISK_AGENT_MCX_CLOSE", "23:55"))


def now_ist() -> datetime:
    fake = os.environ.get("RISK_AGENT_FAKE_NOW")  # tests only
    if fake:
        return datetime.fromisoformat(fake).replace(tzinfo=IST)
    return datetime.now(IST)


def file_mtime_ist(path: Path) -> datetime | None:
    """When the file was really written. Used to catch a forged `saved_at` inside a hand-edited
    config. In tests (fake clock) the real mtime is meaningless, so it is only used if
    RISK_AGENT_FAKE_MTIME is set."""
    fake_now, fake_mtime = os.environ.get("RISK_AGENT_FAKE_NOW"), os.environ.get("RISK_AGENT_FAKE_MTIME")
    if fake_mtime:
        return datetime.fromisoformat(fake_mtime).replace(tzinfo=IST)
    if fake_now:
        return None
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime, IST)
    except OSError:
        return None


def in_edit_window(ts: datetime | None = None) -> bool:
    return (ts or now_ist()).astimezone(IST).time() < EDIT_CUTOFF


def edit_window_text() -> str:
    n = now_ist()
    if in_edit_window(n):
        mins = int((datetime.combine(n.date(), EDIT_CUTOFF, IST) - n).total_seconds() // 60)
        return f"open until 09:00 ({mins} min left)"
    return f"closed - reopens {(n + timedelta(days=1)):%a %d %b} 00:00-09:00"


def read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(data, indent=2, default=str))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def sanitize(cfg: dict | None) -> dict:
    """Defaults + type coercion + clamping. A broken or hostile config can't disable the daily limit."""
    out = dict(DEFAULT_CONFIG)
    for k, v in (cfg or {}).items():
        if k in DEFAULT_CONFIG:
            out[k] = v
    for k, (lo, hi) in BOUNDS.items():
        try:
            out[k] = min(max(float(out[k]), lo), hi)
        except (TypeError, ValueError):
            out[k] = DEFAULT_CONFIG[k]
    out["max_orders_per_day"] = int(out["max_orders_per_day"])
    out["mode"] = out["mode"] if out["mode"] in ("enforce", "alert_only") else "enforce"
    for b in ("include_delivery", "profit_protection_enabled", "anti_revenge_enabled"):
        out[b] = bool(out[b])
    return out


def is_holiday(segment: str, d: date) -> bool:
    """User-maintained calendar in data/holidays.json: {"NSE": ["2026-11-10"], "MCX": [...]}.
    NSE covers CASH and FNO. Empty/missing file means no holidays are known."""
    cal = read_json(HOLIDAYS_PATH, {})
    key = "MCX" if segment == "COMMODITY" else "NSE"
    return d.isoformat() in set(cal.get(key, []))


def market_open(segment: str, ts: datetime | None = None, buffer_min: int = 0) -> bool:
    """Coarse session check. Exits outside these hours would be rejected or turned into AMOs,
    so the executor waits instead of spamming. buffer_min widens the window on both sides."""
    if os.environ.get("RISK_AGENT_IGNORE_MARKET_HOURS") == "1":  # mock mode only
        return True
    ts = (ts or now_ist()).astimezone(IST)
    if ts.weekday() >= 5 or is_holiday(segment, ts.date()):
        return False
    t = ts.replace(tzinfo=None)
    b = timedelta(minutes=buffer_min)
    day = datetime.combine(ts.date(), dtime(0, 0))
    lo, hi = (dtime(9, 0), MCX_CLOSE) if segment == "COMMODITY" else (dtime(9, 15), dtime(15, 30))
    return day + timedelta(hours=lo.hour, minutes=lo.minute) - b <= t <= \
        day + timedelta(hours=hi.hour, minutes=hi.minute) + b


def any_market_open(buffer_min: int = 5) -> bool:
    return any(market_open(s, buffer_min=buffer_min) for s in ("CASH", "COMMODITY"))
