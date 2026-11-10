"""Unattended-login behaviour, tested against a stand-in for the Groww SDK."""
import sys
import types
from datetime import datetime

import pytest

from risk_agent import config as C


class FakeAuthEx(Exception):
    pass


def install_fake_sdk(monkeypatch, fail_times=0):
    state = {"calls": 0, "fail_left": fail_times}

    class FakeAPI:
        def __init__(self, token):
            self.token = token

        @staticmethod
        def get_access_token(api_key, totp=None, secret=None):
            state["calls"] += 1
            if state["fail_left"] > 0:
                state["fail_left"] -= 1
                raise RuntimeError("groww down")
            assert totp and totp.isdigit() and len(totp) == 6, "must log in with a generated 6-digit TOTP"
            return f"token-{state['calls']}"

    exc = types.ModuleType("growwapi.groww.exceptions")
    for n in ("Authentication", "Authorisation", "RateLimit", "Timeout", "BadRequest", "NotFound"):
        setattr(exc, f"GrowwAPI{n}Exception", type(f"GrowwAPI{n}Exception", (Exception,), {}))
    groww = types.ModuleType("growwapi.groww")
    groww.exceptions = exc
    pkg = types.ModuleType("growwapi")
    pkg.GrowwAPI, pkg.groww = FakeAPI, groww
    for name, mod in (("growwapi", pkg), ("growwapi.groww", groww), ("growwapi.groww.exceptions", exc)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setenv("GROWW_API_KEY", "k")
    monkeypatch.setenv("GROWW_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    return state


def ist(s):
    return datetime.fromisoformat(s).replace(tzinfo=C.IST)


def test_token_expiry_follows_6am_reset():
    from risk_agent.brokers import GrowwBroker as G
    assert G.token_expired_at(ist("2026-11-10T05:59"), ist("2026-11-10T06:03"))
    assert not G.token_expired_at(ist("2026-11-10T06:05"), ist("2026-11-10T23:00"))
    assert not G.token_expired_at(ist("2026-11-10T06:05"), ist("2026-11-11T05:59"))
    assert G.token_expired_at(ist("2026-11-10T06:05"), ist("2026-11-11T06:01"))
    assert G.token_expired_at(None, ist("2026-11-10T12:00"))


def test_maintain_refreshes_after_6am_without_a_failed_call(monkeypatch):
    st = install_fake_sdk(monkeypatch)
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-10T23:00")
    from risk_agent.brokers import GrowwBroker
    b = GrowwBroker()
    assert st["calls"] == 1
    b.maintain()
    assert st["calls"] == 1                       # still valid the same evening
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-11T06:01")
    b.maintain()
    assert st["calls"] == 1                       # grace period right after the reset
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-11T06:03")
    b.maintain()
    assert st["calls"] == 2 and b.client.token == "token-2"
    b.maintain()
    assert st["calls"] == 2                       # no repeat logins


def test_failed_login_backs_off_and_does_not_burn_budget(monkeypatch):
    st = install_fake_sdk(monkeypatch, fail_times=1000)
    from risk_agent.brokers import AuthError, GrowwBroker
    alerts = []
    b = GrowwBroker(on_alert=lambda lvl, msg: alerts.append((lvl, msg)))   # must NOT raise
    assert b.client is None and st["calls"] == 1
    for _ in range(50):                            # engine ticking every second
        b.maintain()
        with pytest.raises(AuthError):
            b.auth()
    assert st["calls"] == 1, "backoff must stop a login storm"
    assert b._tokens_today() == 0, "failed attempts must not count against the daily token budget"
    assert len([a for a in alerts]) == 1           # one alert, not fifty


def test_recovers_after_groww_comes_back(monkeypatch):
    st = install_fake_sdk(monkeypatch, fail_times=1)
    from risk_agent.brokers import GrowwBroker
    alerts = []
    b = GrowwBroker(on_alert=lambda lvl, msg: alerts.append(msg))
    assert b.client is None
    b._next_auth_at = 0                            # skip the wait
    b.maintain()
    assert b.client is not None and b._tokens_today() == 1
    assert any("recovered" in m for m in alerts)


def test_concurrent_401s_cause_a_single_relogin(monkeypatch):
    st = install_fake_sdk(monkeypatch)
    from risk_agent.brokers import GrowwBroker
    b = GrowwBroker()
    stale_gen = b._gen
    b.auth(seen_gen=stale_gen)                     # first thread re-logs in
    b.auth(seen_gen=stale_gen)                     # second thread saw the same dead token: no-op
    b.auth(seen_gen=stale_gen)
    assert st["calls"] == 2


def test_daily_token_budget_is_enforced(monkeypatch):
    install_fake_sdk(monkeypatch)
    from risk_agent.brokers import AuthError, GrowwBroker
    b = GrowwBroker()
    C.write_json(C.DATA_DIR / "token_counter.json", {C.now_ist().date().isoformat(): 100})
    with pytest.raises(AuthError, match="budget"):
        b.auth()
