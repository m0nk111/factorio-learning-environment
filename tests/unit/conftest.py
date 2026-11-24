"""Unit tests in this directory run WITHOUT a live Factorio server.

The repo-level tests/conftest.py installs an autouse fixture
(_reset_between_tests) that depends on a running game instance; shadow it
with a no-op here so pure serialization/unit tests stay server-free.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_between_tests():
    yield


@pytest.fixture(autouse=True)
def _no_machine_local_settings(monkeypatch, tmp_path):
    """Keep the suite hermetic: the developer's own ~/.config/fle/mcp.json must not
    decide what these tests attach to."""
    monkeypatch.setenv("FLE_LOCAL_SETTINGS", str(tmp_path / "absent-mcp.json"))
