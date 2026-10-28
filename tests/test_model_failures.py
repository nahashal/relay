"""When the model provider fails, the run must still stop in a named state.

This file exists because it did not. Gemini returned 503 UNAVAILABLE, the
exception escaped plan(), the loop (which catches only PlanningError) never saw
it, and FastAPI returned a 500. The run ended in no stop state at all - which is
the one thing the project promises cannot happen.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from relay import api
from relay.audit import AuditLog
from relay.loop import new_loop
from relay.planner import Planner, PlanningError, explain_model_error
from relay.schemas import StopState

TUE = dt.date(2026, 10, 27)


class Boom:
    """A reader whose model call fails the way a provider fails."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def read(self, goal: str):
        from relay.planner import explain_model_error as explain

        raise PlanningError(explain(self._exc, "test-model"))


class ApiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# The message a person actually reads
# --------------------------------------------------------------------------- #


def test_an_overloaded_model_says_to_try_again():
    msg = explain_model_error(ApiError(503, "UNAVAILABLE high demand"), "m")
    assert "overloaded" in msg
    # The two facts that matter when a run fails mid-goal.
    assert "nothing was sent" in msg.lower()


def test_an_exhausted_quota_is_distinguished_from_a_zero_quota():
    used_up = explain_model_error(ApiError(429, "RESOURCE_EXHAUSTED"), "m")
    assert "used up" in used_up

    paid_only = explain_model_error(ApiError(429, "RESOURCE_EXHAUSTED limit: 0"), "m")
    assert "free tier" in paid_only and "billing" in paid_only


def test_a_rejected_key_names_the_setting():
    assert "GEMINI_API_KEY" in explain_model_error(ApiError(401, "API key invalid"), "m")


def test_an_unknown_model_points_at_the_checker():
    msg = explain_model_error(ApiError(404, "NOT_FOUND"), "ghost-model")
    assert "ghost-model" in msg and "check_setup" in msg


def test_an_unrecognised_failure_still_produces_a_message():
    msg = explain_model_error(RuntimeError("socket exploded"), "m")
    assert "socket exploded" in msg


def test_the_message_does_not_run_away():
    """A wall of SDK JSON in a UI banner helps nobody."""
    msg = explain_model_error(RuntimeError("x" * 5000), "m")
    assert len(msg) < 300


# --------------------------------------------------------------------------- #
# The run still stops properly
# --------------------------------------------------------------------------- #


def test_a_model_failure_ends_the_run_in_blocked(backend, audit):
    planner = Planner(backend=backend, reader=Boom(ApiError(503, "UNAVAILABLE")), today=TUE)
    loop = new_loop(planner, audit, run_id="run_503")

    status = loop.start("anything at all")

    assert status.finished
    assert status.stop_state is StopState.BLOCKED
    assert "overloaded" in status.summary


def test_a_model_failure_sends_nothing(backend, audit):
    planner = Planner(backend=backend, reader=Boom(ApiError(503, "UNAVAILABLE")), today=TUE)
    loop = new_loop(planner, audit, run_id="run_503b")
    loop.start("anything")
    assert backend.sent_count == 0 and backend.event_count == 0


def test_the_stop_state_is_recorded_in_the_audit_log(backend, audit):
    planner = Planner(backend=backend, reader=Boom(ApiError(429, "RESOURCE_EXHAUSTED")), today=TUE)
    loop = new_loop(planner, audit, run_id="run_429")
    loop.start("anything")

    row = audit._db.execute("SELECT stop_state FROM runs WHERE id = ?", ("run_429",)).fetchone()
    assert row["stop_state"] == StopState.BLOCKED.value


# --------------------------------------------------------------------------- #
# And the API returns a result, not a 500
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path, monkeypatch):
    api.configure(AuditLog(tmp_path / "fail.db"))
    api._RUNS.clear()
    monkeypatch.setattr(
        api, "_make_planner", lambda backend: Planner(
            backend=backend, reader=Boom(ApiError(503, "UNAVAILABLE")), today=TUE
        )
    )
    return TestClient(api.app)


def test_the_api_returns_blocked_not_a_500(client):
    """The exact symptom: clicking Plan did nothing, because the server 500'd."""
    response = client.post("/run", json={"goal": "plan a weekly study timetable"})

    assert response.status_code == 200
    body = response.json()
    assert body["stop_state"] == "blocked"
    assert body["awaiting"] == "nothing"
    assert "overloaded" in body["summary"]
