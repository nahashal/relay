"""The UI's contract with the server.

The HTML is not tested here - what matters is that the server hands the browser
everything it needs to render a choice, and that the two narrow edits (pick a
slot, rewrite an email) still go through the approval gate.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import ANU, RAHUL
from relay import api
from relay.audit import AuditLog

EXAMPLE_GOAL = (
    "Email Rahul Menon the Q3 report and set a 30-minute review with him and Anu next week."
)
ANSWER = {"files": {"Q3 report": "f_q3_final"}}


@pytest.fixture
def client(tmp_path) -> TestClient:
    api.configure(AuditLog(tmp_path / "ui.db"))
    api._RUNS.clear()
    return TestClient(api.app)


def _at_email_card(client) -> dict:
    run = client.post("/run", json={"goal": EXAMPLE_GOAL}).json()
    return client.post("/answer", json={"run_id": run["run_id"], "choices": ANSWER}).json()


def _at_event_card(client) -> dict:
    body = _at_email_card(client)
    return client.post(
        "/decide",
        json={
            "run_id": body["run_id"],
            "step_number": body["card"]["step_number"],
            "decision": {"verdict": "approve"},
        },
    ).json()


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #


def test_the_root_serves_the_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "relay" in response.text.lower()


def test_the_ui_declares_no_build_step(client):
    """One file, like the template it borrows from: no bundler, no framework."""
    html = client.get("/").text
    assert "<script src=" not in html  # no external JS
    assert "fonts.googleapis.com" in html  # fonts are the only remote asset


# --------------------------------------------------------------------------- #
# Questions arrive structured, not as prose to parse
# --------------------------------------------------------------------------- #


def test_a_question_carries_pickable_options(client):
    body = client.post("/run", json={"goal": EXAMPLE_GOAL}).json()
    assert body["awaiting"] == "answer"

    ask = body["asks"][0]
    assert ask["kind"] == "file"
    assert ask["key"] == "Q3 report"
    assert [o["value"] for o in ask["options"]] == ["f_q3_final", "f_q3_draft"]
    # And the prose is still there for the terminal and the log.
    assert "Several files match" in body["questions"][0]


def test_a_contact_question_carries_the_distinguishing_detail(client):
    body = client.post("/run", json={"goal": "Email Rahul the Q3 report next week."}).json()
    ask = next(a for a in body["asks"] if a["kind"] == "contact")
    assert ask["key"] == "Rahul"
    labels = [o["label"] for o in ask["options"]]
    assert any("Finance" in label for label in labels)
    assert any("Design team" in label for label in labels)
    assert RAHUL in [o["value"] for o in ask["options"]]


def test_an_answer_built_from_the_options_is_accepted(client):
    """Exactly what the browser does: read options, post back one value."""
    body = client.post("/run", json={"goal": EXAMPLE_GOAL}).json()
    ask = body["asks"][0]
    choices = {"files": {ask["key"]: ask["options"][1]["value"]}}

    body = client.post("/answer", json={"run_id": body["run_id"], "choices": choices}).json()
    assert body["awaiting"] == "approval"
    assert body["draft"]["attachments"] == ["f_q3_draft"]


def test_a_question_with_no_candidates_says_so(client):
    """The UI needs to know when to show a text box instead of radios."""
    body = client.post("/run", json={"goal": "Email Zoltan the Q3 report next week."}).json()
    ask = next(a for a in body["asks"] if a["kind"] == "contact")
    assert ask["options"] == []


# --------------------------------------------------------------------------- #
# The slot picker
# --------------------------------------------------------------------------- #


def test_the_event_card_offers_every_slot_found(client):
    body = _at_event_card(client)
    assert body["card"]["action"]["tool"] == "create_event"
    assert len(body["slot_options"]) > 1
    # The proposed slot is one of them.
    assert body["card"]["action"]["args"]["slot"] in body["slot_options"]


def test_picking_another_slot_needs_fresh_approval(client):
    body = _at_event_card(client)
    run_id = body["run_id"]
    other = body["slot_options"][1]

    revised = dict(body["card"]["action"]["args"])
    revised["slot"] = other
    body = client.post(
        "/decide",
        json={
            "run_id": run_id,
            "step_number": body["card"]["step_number"],
            "decision": {
                "verdict": "edit",
                "revised_action": {"tool": "create_event", "args": revised},
            },
        },
    ).json()

    # Still waiting, now on the edited slot.
    assert body["awaiting"] == "approval"
    assert body["card"]["action"]["args"]["slot"] == other

    body = client.post(
        "/decide",
        json={
            "run_id": run_id,
            "step_number": body["card"]["step_number"],
            "decision": {"verdict": "approve"},
        },
    ).json()
    assert body["stop_state"] == "done"


def test_the_email_card_offers_no_slots(client):
    body = _at_email_card(client)
    assert body["slot_options"] == []
    assert body["draft"]["to"] == [RAHUL]


# --------------------------------------------------------------------------- #
# Editing an email makes a new draft
# --------------------------------------------------------------------------- #


def test_redraft_returns_a_new_draft_id(client):
    body = _at_email_card(client)
    before = body["card"]["action"]["args"]["draft_id"]

    redrafted = client.post(
        "/redraft",
        json={
            "run_id": body["run_id"],
            "to": [RAHUL, ANU],
            "subject": "Q3 report, final",
            "body": "Hi both, attaching it.",
            "attachments": ["f_q3_final"],
        },
    ).json()
    assert redrafted["draft_id"] != before


def test_an_edited_email_must_be_approved_again(client):
    body = _at_email_card(client)
    run_id = body["run_id"]

    redrafted = client.post(
        "/redraft",
        json={"run_id": run_id, "to": [ANU], "subject": "Moved", "body": "hi", "attachments": []},
    ).json()

    body = client.post(
        "/decide",
        json={
            "run_id": run_id,
            "step_number": body["card"]["step_number"],
            "decision": {
                "verdict": "edit",
                "revised_action": {
                    "tool": "send_email",
                    "args": {"draft_id": redrafted["draft_id"]},
                },
            },
        },
    ).json()

    assert body["awaiting"] == "approval"  # the edit sent nothing
    assert body["draft"]["to"] == [ANU]

    trail = client.get(f"/audit/{run_id}").json()
    sent = [c for c in trail["tool_calls"] if c["tool"] == "send_email"]
    assert sent == []  # genuinely nothing sent yet


def test_redraft_is_refused_when_no_email_is_pending(client):
    body = client.post("/run", json={"goal": EXAMPLE_GOAL}).json()
    response = client.post(
        "/redraft", json={"run_id": body["run_id"], "to": [RAHUL], "subject": "x", "body": "y"}
    )
    assert response.status_code == 409


def test_redraft_rejects_an_empty_recipient_list(client):
    body = _at_email_card(client)
    response = client.post(
        "/redraft", json={"run_id": body["run_id"], "to": [], "subject": "x", "body": "y"}
    )
    assert response.status_code == 422


def test_redraft_costs_a_tool_call(client):
    """It is audited like any other tool use, even though it needs no approval."""
    body = _at_email_card(client)
    before = body["tool_calls_used"]
    client.post(
        "/redraft",
        json={"run_id": body["run_id"], "to": [ANU], "subject": "x", "body": "y"},
    )
    after = client.get(f"/status/{body['run_id']}").json()
    assert after["tool_calls_used"] == before + 1
