import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pytest


@pytest.fixture(autouse=True)
def offline_corescope(monkeypatch):
    """No test talks to a real CoreScope: by default every community call
    finds it unreachable (tests that need answers patch these again), and test
    fast mode is off unless a test turns it on."""
    from openhop_corescope_observer_plus import corescope

    monkeypatch.delenv("OPENHOP_OBSERVER_PLUS_TEST_FAST", raising=False)
    monkeypatch.delenv("OPENHOP_CONFIG_PATH", raising=False)
    monkeypatch.setattr(corescope, "policy", lambda url, obs, ver, **k: (0, None))
    monkeypatch.setattr(corescope, "battery_check", lambda url, nid, **k: (False, {}))
    monkeypatch.setattr(corescope, "owners", lambda url, obs, **k: None)
    monkeypatch.setattr(corescope, "monitors", lambda url, nid, ver, **k: (0, None))


@pytest.fixture(autouse=True)
def no_startup_hold(request, monkeypatch):
    """Scheduled polls in tests start at once; the startup hold has its own
    tests (marked startup_hold)."""
    if request.node.get_closest_marker("startup_hold"):
        return
    from openhop_corescope_observer_plus.coordination import Coordinator

    monkeypatch.setattr(Coordinator, "starting", lambda self, now: False)
    monkeypatch.setattr(Coordinator, "standby_hold_from", lambda self: 0.0)


def pytest_configure(config):
    config.addinivalue_line("markers", "startup_hold: keep the plugin's startup hold on")
