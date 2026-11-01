import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pytest


@pytest.fixture(autouse=True)
def offline_corescope(monkeypatch):
    """No test talks to a real CoreScope: by default every community call
    finds it unreachable (tests that need answers patch these again), and test
    fast mode is off unless a test turns it on."""
    from openhop_neighbour_battery import app as app_mod
    monkeypatch.delenv("OPENHOP_NEIGHBOUR_BATTERY_TEST_FAST", raising=False)
    monkeypatch.delenv("OPENHOP_CONFIG_PATH", raising=False)
    monkeypatch.setattr(app_mod, "corescope_policy", lambda url, obs, ver, **k: (0, None))
    monkeypatch.setattr(app_mod, "corescope_battery_check", lambda url, nid, **k: (False, {}))
    monkeypatch.setattr(app_mod, "corescope_owners", lambda url, obs, **k: None)
    monkeypatch.setattr(app_mod, "corescope_monitors", lambda url, nid, ver, **k: (0, None))
