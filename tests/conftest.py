import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="riskagent-test-")
os.environ["RISK_AGENT_DATA"] = _TMP
os.environ["RISK_AGENT_IGNORE_MARKET_HOURS"] = "1"
os.environ["RISK_AGENT_EXIT_TIMEOUT"] = "0"        # treat any non-filled exit as stuck immediately in tests
os.environ["RISK_AGENT_RETRY_BACKOFF"] = "0"
os.environ["RISK_AGENT_ORDERS_POLL_SECONDS"] = "0"  # simulated steps are faster than wall clock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_data(monkeypatch):
    for p in Path(_TMP).iterdir():
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    monkeypatch.setenv("RISK_AGENT_FAKE_NOW", "2026-11-10T10:00")  # a Tuesday, market hours
    monkeypatch.delenv("RISK_AGENT_FAKE_MTIME", raising=False)
    yield
