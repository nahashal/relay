"""What the approval card says about a meeting.

A meeting card used to show two lines of arguments: the title, the times and
the attendees. It did not show the email those attendees would receive, and
it offered no way to change any of it - the only control was a radio list of
"other free slots", which could only ever offer times the search had already
found.

Approving a send you cannot read is the thing the gate exists to prevent.
"""

from __future__ import annotations

import datetime as dt

import pytest

from relay.schemas import Action, ToolName
from relay.tools import MockBackend
from relay.tools.google_backend import GoogleBackend
from relay.tools.local_backend import (
    LocalBackend,
    invitation_subject,
    invitation_text,
)

SLOT = {"start": "2026-10-29T12:00:00+05:30", "end": "2026-10-29T12:30:00+05:30"}
GUESTS = ["me@example.com", "fayaz@example.com"]


def _loop(backend, audit, run_id):
    """A loop over this backend. The loop and executor must share one, which
    is why new_loop takes the planner rather than a backend."""
    from relay.loop import new_loop
    from relay.planner import Planner, RegexIntentReader

    return new_loop(
        Planner(backend=backend, reader=RegexIntentReader()), audit, run_id=run_id
    )


# --------------------------------------------------------------------------- #
# Every backend answers for itself
# --------------------------------------------------------------------------- #


def test_every_backend_describes_its_own_event():
    """One protocol, three honest answers - the mock must not imply a send,
    and Google must not imply a RELAY-composed email."""
    for backend in (MockBackend, LocalBackend, GoogleBackend):
        assert callable(getattr(backend, "describe_event", None)), backend.__name__


def test_the_mock_says_it_sends_nothing():
    text = MockBackend().describe_event(SLOT, GUESTS, "Review")
    assert "no invitation is actually sent" in text


# --------------------------------------------------------------------------- #
# The preview must BE the email, not merely resemble it
# --------------------------------------------------------------------------- #


def test_the_preview_uses_the_text_that_is_actually_sent(local_backend):
    """The anti-drift property. A preview that is merely similar to the real
    invitation is worse than none, because it would be trusted."""
    start = dt.datetime(2026, 10, 29, 12, 0)
    end = dt.datetime(2026, 10, 29, 12, 30)
    preview = local_backend.describe_event(SLOT, GUESTS, "Review")

    assert invitation_subject("Review", start) in preview
    assert invitation_text("Review", start, end) in preview


def test_the_preview_names_the_guests_and_the_copy_to_you(local_backend):
    preview = local_backend.describe_event(SLOT, GUESTS, "Review")
    assert "fayaz@example.com" in preview
    assert "me@example.com" in preview
    assert "invite.ics" in preview


def test_a_meeting_with_only_you_still_describes_itself(local_backend):
    """"Block an hour on Thursday" has no guests, and must not crash the card."""
    preview = local_backend.describe_event(SLOT, ["me@example.com"], "Focus time")
    assert "Focus time" in preview
    assert "Invitation to:" not in preview


# --------------------------------------------------------------------------- #
# It reaches the card
# --------------------------------------------------------------------------- #


def test_the_card_carries_the_invitation(backend, audit):
    loop = _loop(backend, audit, "card")
    action = Action(
        tool=ToolName.CREATE_EVENT,
        args={"slot": SLOT, "attendees": GUESTS, "title": "Review"},
    )
    detail = loop._detail(action)

    # The arguments, as before...
    assert "Review" in detail and "fayaz@example.com" in detail
    # ...and now what the recipients actually get.
    assert "no invitation is actually sent" in detail


def test_a_failing_preview_does_not_take_down_the_card(backend, audit, monkeypatch):
    """A preview is a courtesy. A card that vanished because it could not be
    rendered would be a far worse bug - and a SILENT failure is how a missing
    preview came to look exactly like a working one."""
    from relay.loop import new_loop

    def boom(*args, **kwargs):
        raise RuntimeError("calendar unreachable")

    monkeypatch.setattr(backend, "describe_event", boom)
    loop = _loop(backend, audit, "card2")
    detail = loop._detail(
        Action(
            tool=ToolName.CREATE_EVENT,
            args={"slot": SLOT, "attendees": GUESTS, "title": "Review"},
        )
    )

    assert "Review" in detail
    assert "could not render the invitation" in detail
    assert "calendar unreachable" in detail


@pytest.fixture
def local_backend(tmp_path):
    from relay.brevo import BrevoClient

    class _Transport:
        def __call__(self, method, url, headers, body):
            return 201, {"messageId": "<x@relay>"}

    return LocalBackend(
        me="me@example.com",
        documents=tmp_path,
        contacts=tmp_path / "c.json",
        brevo=BrevoClient(
            api_key="k", sender_email="me@example.com", transport=_Transport()
        ),
    )


# --------------------------------------------------------------------------- #
# Reject & replan has to change something
# --------------------------------------------------------------------------- #


def test_rejecting_a_meeting_replans_onto_a_different_slot(backend, audit):
    """It used to throw the rejection away and replan from identical inputs,
    so it handed back an identical card: a replan spent on nothing."""
    from relay.schemas import Decision

    loop = _loop(backend, audit, "replan")
    status = loop.start("Book a 30-minute review with Rahul Menon next week.")
    if status.awaiting.value == "answer":
        from relay.planner import Choices

        status = loop.answer(Choices(contacts={"Rahul Menon": "rahul.menon@example.com"}))

    first = status.card.action.args["slot"]
    status = loop.decide(
        status.card.step_number, Decision(verdict="reject", request_revision=True)
    )

    assert status.card is not None, "a revision must come back with a new card"
    assert status.card.action.args["slot"] != first


def test_rejecting_twice_keeps_moving(backend, audit):
    from relay.planner import Choices
    from relay.schemas import Decision

    loop = _loop(backend, audit, "replan2")
    status = loop.start("Book a 30-minute review with Rahul Menon next week.")
    if status.awaiting.value == "answer":
        status = loop.answer(Choices(contacts={"Rahul Menon": "rahul.menon@example.com"}))

    seen = []
    for _ in range(2):
        if status.card is None:
            break
        seen.append(status.card.action.args["slot"])
        status = loop.decide(
            status.card.step_number, Decision(verdict="reject", request_revision=True)
        )

    assert len(seen) == len({str(s) for s in seen}), f"a slot was offered twice: {seen}"


def test_a_plain_rejection_does_not_move_the_slot(backend, audit):
    """Only a revision means "not that time". A plain reject ends the run."""
    from relay.planner import Choices
    from relay.schemas import Decision, StopState

    loop = _loop(backend, audit, "replan3")
    status = loop.start("Book a 30-minute review with Rahul Menon next week.")
    if status.awaiting.value == "answer":
        status = loop.answer(Choices(contacts={"Rahul Menon": "rahul.menon@example.com"}))

    status = loop.decide(status.card.step_number, Decision(verdict="reject"))
    assert status.stop_state is StopState.REJECTED
    assert loop.slot_offset == 0
