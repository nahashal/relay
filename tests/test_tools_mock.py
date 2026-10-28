"""Tool-layer tests: each tool behaves, and the awkward scenarios are reachable.

These cover the mocked half of build step 2's exit check ("each tool passes a
unit test, live and mocked"). The live half needs the test Google account.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import ANU, ME, RAHUL, WINDOW_END, WINDOW_START
from relay.tools import MockBackend, ToolError

PRIYA = "priya.nair@relay-test.example"


def test_two_rahuls_are_both_returned(backend: MockBackend):
    """Scenario: two people with the same name. The tool must not choose."""
    matches = backend.find_contact("Rahul")
    assert [m["note"] for m in matches] == ["Finance", "Design team"]


def test_full_name_disambiguates(backend: MockBackend):
    assert len(backend.find_contact("Rahul Shetty")) == 1


def test_no_matching_contact(backend: MockBackend):
    assert backend.find_contact("Zoltan") == []


def test_several_files_match_newest_first(backend: MockBackend):
    """Scenario: several matching files. Newest first, but still two answers."""
    matches = backend.find_file("Q3 report")
    assert [m["id"] for m in matches] == ["f_q3_final", "f_q3_draft"]


def test_no_matching_file(backend: MockBackend):
    assert backend.find_file("Q5 report") == []


def test_free_slots_respect_everyones_calendar(backend: MockBackend):
    slots = backend.find_free_slots([ME, RAHUL, ANU], 30, WINDOW_START, WINDOW_END)
    assert slots, "the fixture should leave at least one common slot"
    # Mon 2 Nov is booked solid for all three.
    assert all(not s["start"].startswith("2026-11-02") for s in slots)
    # Every slot is 30 minutes and inside working hours.
    for s in slots:
        start, end = dt.datetime.fromisoformat(s["start"]), dt.datetime.fromisoformat(s["end"])
        assert end - start == dt.timedelta(minutes=30)
        assert 9 <= start.hour < 18


def test_no_common_slot_is_reported_not_invented(backend: MockBackend):
    """Scenario: no free slot next week. Priya is busy all week -> empty list."""
    assert backend.find_free_slots([ME, RAHUL, ANU, PRIYA], 30, WINDOW_START, WINDOW_END) == []


def test_unknown_attendee_raises_rather_than_assuming_free(backend: MockBackend):
    with pytest.raises(ToolError):
        backend.find_free_slots([ME, "stranger@example.com"], 30, WINDOW_START, WINDOW_END)


def test_weekends_are_never_offered(backend: MockBackend):
    slots = backend.find_free_slots([ME], 30, dt.date(2026, 11, 7), dt.date(2026, 11, 8))
    assert slots == []


def test_draft_rejects_unknown_attachment(backend: MockBackend):
    with pytest.raises(ToolError):
        backend.draft_email([RAHUL], "Q3", "hi", ["f_does_not_exist"])


def test_draft_requires_a_recipient(backend: MockBackend):
    with pytest.raises(ToolError):
        backend.draft_email([], "Q3", "hi", [])


def test_booking_an_event_makes_people_busy(backend: MockBackend):
    """Reality check: after booking, the same slot must stop being offered."""
    slots = backend.find_free_slots([ME, RAHUL, ANU], 30, WINDOW_START, WINDOW_END)
    chosen = slots[0]
    backend.create_event(chosen, [ME, RAHUL, ANU], "Q3 review")
    assert chosen not in backend.find_free_slots([ME, RAHUL, ANU], 30, WINDOW_START, WINDOW_END)


def test_staged_api_failure_happens_once():
    """Scenario: API failure mid-run, then a retry that succeeds."""
    backend = MockBackend(fail_on={"find_contact"})
    with pytest.raises(ToolError):
        backend.find_contact("Anu")
    assert len(backend.find_contact("Anu")) == 1  # the retry works


def test_verification_reads_state_back(backend: MockBackend):
    draft = backend.draft_email([RAHUL], "Q3", "hi", ["f_q3_final"])
    receipt = backend.send_email(draft["draft_id"])
    assert backend.message_in_sent(receipt["message_id"])
    assert not backend.message_in_sent("msg_not_real")
