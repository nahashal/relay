"""End-to-end over HTTP: plan, answer, approve, send - and nothing unapproved."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import RAHUL
from relay import api
from relay.audit import AuditLog

EXAMPLE_GOAL = (
    "Email Rahul Menon the Q3 report and set a 30-minute review with him and Anu next week."
)
ANSWER = {"files": {"Q3 report": "f_q3_final"}}


@pytest.fixture
def client(tmp_path) -> TestClient:
    # configure() exists so the app never creates a database in the repo root.
    api.configure(AuditLog(tmp_path / "api.db"))
    api._RUNS.clear()
    return TestClient(api.app)


def _answered_run(client) -> dict:
    run = client.post("/run", json={"goal": EXAMPLE_GOAL}).json()
    return client.post("/answer", json={"run_id": run["run_id"], "choices": ANSWER}).json()


# --------------------------------------------------------------------------- #
# Planning and questions
# --------------------------------------------------------------------------- #


def test_run_pauses_for_an_answer_when_the_goal_is_ambiguous(client):
    body = client.post("/run", json={"goal": EXAMPLE_GOAL}).json()
    assert body["awaiting"] == "answer"
    assert body["stop_state"] == "needs_input"
    assert "Several files match" in body["questions"][0]
    assert body["card"] is None


def test_answering_resumes_and_offers_the_first_card(client):
    body = _answered_run(client)
    assert body["awaiting"] == "approval"
    assert body["card"]["action"]["tool"] == "send_email"
    assert body["window"] == "Mon 02 Nov 2026 to Fri 06 Nov 2026"
    assert [s["tool"] for s in body["plan"]] == [
        "draft_email",
        "send_email",
        "find_free_slots",
        "create_event",
        "finish",
    ]


def test_choices_can_be_supplied_up_front(client):
    body = client.post("/run", json={"goal": EXAMPLE_GOAL, "choices": ANSWER}).json()
    assert body["awaiting"] == "approval"
    assert body["replans_used"] == 0  # no round trip needed


def test_a_goal_with_no_tool_step_returns_a_checklist(client):
    body = client.post("/run", json={"goal": "Tidy up my desk."}).json()
    assert body["stop_state"] == "done"
    assert [s["tool"] for s in body["plan"]] == ["note", "finish"]


# --------------------------------------------------------------------------- #
# The full approved run
# --------------------------------------------------------------------------- #


def test_the_example_goal_runs_end_to_end_over_http(client):
    body = _answered_run(client)
    run_id = body["run_id"]

    while body["awaiting"] == "approval":
        body = client.post(
            "/decide",
            json={
                "run_id": run_id,
                "step_number": body["card"]["step_number"],
                "decision": {"verdict": "approve"},
            },
        ).json()

    assert body["stop_state"] == "done"
    assert "the email was sent" in body["summary"]


def test_the_card_shows_the_resolved_email(client):
    body = _answered_run(client)
    assert RAHUL in body["card"]["detail"]
    # The draft id on the card is a real id, not a reference.
    assert body["card"]["action"]["args"]["draft_id"].startswith("draft_")


def test_rejecting_stops_the_run(client):
    body = _answered_run(client)
    body = client.post(
        "/decide",
        json={
            "run_id": body["run_id"],
            "step_number": body["card"]["step_number"],
            "decision": {"verdict": "reject", "note": "wrong attachment"},
        },
    ).json()
    assert body["stop_state"] == "rejected"


def test_deciding_the_wrong_step_is_refused(client):
    body = _answered_run(client)
    response = client.post(
        "/decide",
        json={
            "run_id": body["run_id"],
            "step_number": 99,
            "decision": {"verdict": "approve"},
        },
    )
    assert response.status_code == 409


def test_deciding_when_nothing_is_pending_is_refused(client):
    body = client.post("/run", json={"goal": EXAMPLE_GOAL}).json()
    response = client.post(
        "/decide",
        json={"run_id": body["run_id"], "step_number": 1, "decision": {"verdict": "approve"}},
    )
    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# The evidence
# --------------------------------------------------------------------------- #


def test_the_audit_trail_shows_zero_unapproved_side_effects(client):
    body = _answered_run(client)
    run_id = body["run_id"]
    while body["awaiting"] == "approval":
        body = client.post(
            "/decide",
            json={
                "run_id": run_id,
                "step_number": body["card"]["step_number"],
                "decision": {"verdict": "approve"},
            },
        ).json()

    trail = client.get(f"/audit/{run_id}").json()
    assert trail["unapproved_side_effects"] == 0
    approved = [c for c in trail["tool_calls"] if c["approved"]]
    assert {c["tool"] for c in approved} == {"send_email", "create_event"}


def test_status_does_not_advance_the_run(client):
    body = _answered_run(client)
    first = client.get(f"/status/{body['run_id']}").json()
    second = client.get(f"/status/{body['run_id']}").json()
    assert first["completed_steps"] == second["completed_steps"]
    assert first["awaiting"] == "approval"


def test_no_endpoint_executes_an_arbitrary_action(client):
    """The API offers no way to run a tool by name - only to answer or decide."""
    paths = {route.path for route in api.app.routes}
    assert paths >= {"/run", "/answer", "/decide", "/status/{run_id}", "/audit/{run_id}"}
    assert not any("execute" in p or "/tool" in p for p in paths)
