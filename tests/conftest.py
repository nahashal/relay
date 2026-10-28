"""Shared fixtures. Each test gets its own throwaway database and backend."""

from __future__ import annotations

import datetime as dt

import pytest

from relay.audit import AuditLog
from relay.executor import Executor
from relay.schemas import Action, ToolName
from relay.tools import MockBackend

#: The window from the example goal: Mon 2 to Fri 6 Nov 2026.
WINDOW_START = dt.date(2026, 11, 2)
WINDOW_END = dt.date(2026, 11, 6)

RAHUL = "rahul.menon@relay-test.example"
ANU = "anu.prakash@relay-test.example"
ME = "guppy@relay-test.example"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Keep every test off the network, whatever is in .env.

    Loading .env was a real improvement for running RELAY and a trap for testing
    it. Twice over: with GEMINI_API_KEY set, api.py builds the Gemini reader and
    the HTTP tests start making real model calls - slow, flaky, and quietly
    spending the user's quota. With BREVO_API_KEY set, the tests asserting that a
    MISSING key is refused were handed a real one and failed.

    The suite must behave identically on a machine with credentials and one
    without, so they are stripped here rather than in each test.

    A test that wants the live reader can set them back with its own monkeypatch.
    """
    # Which backend the suite runs against is the suite's decision, not the
    # developer's .env. Switching .env to RELAY_BACKEND=local once broke 27
    # tests at a stroke: the backend could not build without the Brevo
    # credentials this same fixture strips.
    monkeypatch.setenv("RELAY_BACKEND", "mock")
    monkeypatch.delenv("RELAY_DRY_RUN", raising=False)

    for name in (
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        # Added after the same mistake twice: a real Brevo key in .env made the
        # "a missing key is refused" tests pass a real key instead. Any
        # credential the code reads from the environment belongs in this list.
        "BREVO_API_KEY",
        "BREVO_SENDER_EMAIL",
        "BREVO_SENDER_NAME",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def backend() -> MockBackend:
    return MockBackend()


@pytest.fixture
def audit(tmp_path) -> AuditLog:
    # tmp_path gives every test a fresh SQLite file, so the duplicate-send
    # guard is tested in isolation rather than against yesterday's rows.
    log = AuditLog(tmp_path / "test.db")
    yield log
    log.close()


@pytest.fixture
def executor(backend: MockBackend, audit: AuditLog) -> Executor:
    ex = Executor(backend=backend, audit=audit, run_id="run_test")
    audit.start_run(ex.run_id, "test goal")
    return ex


@pytest.fixture
def draft_id(backend: MockBackend) -> str:
    draft = backend.draft_email(
        to=[RAHUL],
        subject="Q3 report",
        body="Hi Rahul, attaching the Q3 report.",
        attachments=["f_q3_final"],
    )
    return draft["draft_id"]


def send_action(draft_id: str) -> Action:
    return Action(tool=ToolName.SEND_EMAIL, args={"draft_id": draft_id})


def event_action(slot: dict, attendees: list[str]) -> Action:
    return Action(
        tool=ToolName.CREATE_EVENT,
        args={"slot": slot, "attendees": attendees, "title": "Q3 review"},
    )
