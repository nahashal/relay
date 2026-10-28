"""Backend selection: live is the mode you have to ask for.

These are short tests for a short module, and they are here because the failure
they prevent is expensive: a run that was supposed to be a rehearsal quietly
emailing a real person, or a live demo quietly mocking everything.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from relay import api
from relay.audit import AuditLog
from relay.backends import BackendError, backend_name, is_live, make_backend
from relay.tools import MockBackend


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("RELAY_BACKEND", "RELAY_GOOGLE_ACCOUNT", "RELAY_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)


def test_the_default_is_mock():
    assert backend_name() == "mock"
    assert is_live() is False
    assert isinstance(make_backend(), MockBackend)


def test_mock_is_explicit_too(monkeypatch):
    monkeypatch.setenv("RELAY_BACKEND", "mock")
    assert isinstance(make_backend(), MockBackend)


def test_the_value_is_case_and_space_tolerant(monkeypatch):
    monkeypatch.setenv("RELAY_BACKEND", "  MOCK ")
    assert backend_name() == "mock"


def test_a_misspelt_value_is_an_error_not_a_quiet_mock(monkeypatch):
    """'RELAY_BACKEND=liv' silently mocking would waste a demo."""
    monkeypatch.setenv("RELAY_BACKEND", "liv")
    with pytest.raises(BackendError, match="must be 'mock', 'local' or 'live'"):
        make_backend()


def test_live_without_an_account_address_is_refused(monkeypatch):
    """Everything in a plan is addressed relative to 'me'; guessing is not an option."""
    monkeypatch.setenv("RELAY_BACKEND", "live")
    with pytest.raises(BackendError, match="RELAY_GOOGLE_ACCOUNT"):
        make_backend()


def test_live_without_credentials_is_refused_not_downgraded(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_BACKEND", "live")
    monkeypatch.setenv("RELAY_GOOGLE_ACCOUNT", "guppy@relay-test.example")
    monkeypatch.setenv("RELAY_TOKEN_FILE", str(tmp_path / "nope.json"))
    with pytest.raises(BackendError, match="no usable credentials"):
        make_backend()


def test_credentials_lying_around_do_not_turn_live_on(monkeypatch, tmp_path):
    """The presence of a token file must not be read as consent to go live."""
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RELAY_TOKEN_FILE", str(token))
    assert isinstance(make_backend(), MockBackend)


# --------------------------------------------------------------------------- #
# What the UI is told
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path) -> TestClient:
    api.configure(AuditLog(tmp_path / "mode.db"))
    api._RUNS.clear()
    return TestClient(api.app)


def test_mode_reports_mock_by_default(client):
    assert client.get("/mode").json() == {
        "backend": "mock",
        "live": False,
        "dry_run": False,
    }


def test_dry_run_is_off_unless_asked_for(monkeypatch):
    from relay.backends import is_dry_run

    monkeypatch.delenv("RELAY_DRY_RUN", raising=False)
    assert is_dry_run() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_dry_run_accepts_the_usual_spellings(monkeypatch, value):
    from relay.backends import is_dry_run

    monkeypatch.setenv("RELAY_DRY_RUN", value)
    assert is_dry_run() is True


def test_a_dry_run_is_not_live_even_on_a_real_backend(monkeypatch):
    """The whole point: your own files, and nothing leaves the machine."""
    from relay.backends import is_dry_run, is_live

    monkeypatch.setenv("RELAY_BACKEND", "local")
    monkeypatch.setenv("RELAY_DRY_RUN", "true")
    assert is_dry_run() is True
    assert is_live() is False  # so the UI shows no "this will really send" banner


def test_mode_reports_a_dry_run(client, monkeypatch):
    monkeypatch.setenv("RELAY_BACKEND", "local")
    monkeypatch.setenv("RELAY_DRY_RUN", "true")
    body = client.get("/mode").json()
    assert body == {"backend": "local", "live": False, "dry_run": True}


def test_mode_reports_live_when_configured(client, monkeypatch):
    monkeypatch.setenv("RELAY_BACKEND", "live")
    assert client.get("/mode").json()["live"] is True


def test_the_ui_carries_a_live_warning_banner(client):
    """It must be in the page, not something a future edit can quietly drop."""
    html = client.get("/").text
    assert 'id="live-banner"' in html
    assert "real email" in html


def test_a_broken_live_config_fails_the_run_rather_than_mocking_it(client, monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_BACKEND", "live")
    monkeypatch.setenv("RELAY_GOOGLE_ACCOUNT", "guppy@relay-test.example")
    monkeypatch.setenv("RELAY_TOKEN_FILE", str(tmp_path / "nope.json"))

    response = client.post("/run", json={"goal": "Email Rahul Menon the Q3 report."})
    assert response.status_code == 503
    assert "credentials" in response.json()["detail"]
