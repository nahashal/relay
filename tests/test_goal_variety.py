"""Goals in plain language, not just the example goal.

The brief is "an agent that takes a goal in plain language", so the shapes below
are the deliverable, not edge cases. These run on the offline reader so they are
deterministic; the Gemini reader handles strictly more phrasing, never less.

Each of these started as a real defect found by running varied goals through the
live model - see LESSON 36.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import ANU, RAHUL
from relay.planner import (
    Choices,
    EmailIntent,
    GoalIntent,
    MeetingIntent,
    Planner,
    PlanningError,
    RegexIntentReader,
)
from relay.schemas import ToolName

TUE = dt.date(2026, 10, 27)
ANSWERED = Choices(files={"Q3 report": "f_q3_final"})


@pytest.fixture
def planner(backend):
    return Planner(backend=backend, reader=RegexIntentReader(), today=TUE)


def _tools(plan):
    return [s.action.tool for s in plan.steps]


class FixedReader:
    """Feeds the planner an exact intent, so these test the PLANNER not the regex."""

    def __init__(self, intent: GoalIntent) -> None:
        self._intent = intent

    def read(self, goal: str) -> GoalIntent:
        return self._intent


# --------------------------------------------------------------------------- #
# Refusing, rather than reshaping into the nearest supported action
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "goal",
    [
        "Delete all my emails from Rahul.",
        "Archive everything from Anu.",
        "Forward the Q3 report to Anu Prakash.",
        "Cancel the review with Rahul Menon.",
        "Reschedule Thursday's meeting.",
        "Unsubscribe me from the newsletter.",
        "Book a room for Friday.",
    ],
)
def test_an_unsupported_goal_is_refused(planner, goal):
    """The serious one: "delete my emails from Rahul" must NOT become an email
    to Rahul. A model asked to describe anything in terms of {email, meeting}
    will describe it as one of those unless refusing is an option."""
    with pytest.raises(PlanningError, match="cannot do that"):
        planner.plan(goal, ANSWERED)


def test_the_refusal_names_what_was_asked(planner):
    with pytest.raises(PlanningError, match="delete"):
        planner.plan("Delete all my emails from Rahul.", ANSWERED)


def test_the_refusal_reads_cleanly(backend):
    """The user's words usually end in a full stop; two in a row look like a typo."""
    planner = Planner(
        backend=backend,
        reader=FixedReader(GoalIntent(unsupported=["Forward the Q3 report to Anu."])),
        today=TUE,
    )
    with pytest.raises(PlanningError) as caught:
        planner.plan("anything", ANSWERED)
    assert "Anu. It can send" in str(caught.value)


def test_a_supported_goal_containing_an_awkward_word_still_works(planner):
    """'a 45-minute call' is a meeting. An earlier blocklist refused it."""
    plan = planner.plan("Set up a 45-minute call with Anu Prakash next week.")
    assert _tools(plan) == [ToolName.FIND_FREE_SLOTS, ToolName.CREATE_EVENT, ToolName.FINISH]


# --------------------------------------------------------------------------- #
# How many meetings were actually asked for
# --------------------------------------------------------------------------- #


def test_two_meetings_produce_two_bookings(backend):
    """Silently booking one of two was a real bug: the count was dropped."""
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(meeting=MeetingIntent(attendee_names=["Anu Prakash"], count=2))
        ),
        today=TUE,
    )
    plan = planner.plan("two catch-ups with Anu next week")
    assert _tools(plan).count(ToolName.CREATE_EVENT) == 2
    # One search feeds both, and they take DIFFERENT slots.
    assert _tools(plan).count(ToolName.FIND_FREE_SLOTS) == 1
    indexes = [
        s.action.refs[0].index for s in plan.steps if s.action.tool is ToolName.CREATE_EVENT
    ]
    assert indexes == [0, 1]


def test_each_extra_meeting_is_numbered_for_the_approval_card(backend):
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(meeting=MeetingIntent(attendee_names=["Anu Prakash"], count=2))
        ),
        today=TUE,
    )
    plan = planner.plan("two catch-ups")
    bookings = [s for s in plan.steps if s.action.tool is ToolName.CREATE_EVENT]
    assert "(1 of 2)" in bookings[0].intent and "(2 of 2)" in bookings[1].intent


def test_one_meeting_is_not_numbered(planner):
    plan = planner.plan("Set up a 30-minute review with Anu Prakash next week.")
    booking = next(s for s in plan.steps if s.action.tool is ToolName.CREATE_EVENT)
    assert "(1 of 1)" not in booking.intent


def test_too_many_meetings_is_refused_not_truncated(backend):
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(meeting=MeetingIntent(attendee_names=["Anu Prakash"], count=9))
        ),
        today=TUE,
    )
    with pytest.raises(PlanningError, match="at most"):
        planner.plan("nine meetings")


# --------------------------------------------------------------------------- #
# A meeting has to be a meeting
# --------------------------------------------------------------------------- #


def test_a_meeting_with_nobody_is_refused(backend):
    """"Book a meeting room for Friday" parsed as a meeting with no attendees."""
    planner = Planner(
        backend=backend,
        reader=FixedReader(GoalIntent(meeting=MeetingIntent(attendee_names=[]))),
        today=TUE,
    )
    with pytest.raises(PlanningError, match="no attendees"):
        planner.plan("book a room")


def test_a_zero_length_meeting_is_refused(backend):
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(meeting=MeetingIntent(attendee_names=["Anu Prakash"], minutes=0))
        ),
        today=TUE,
    )
    with pytest.raises(PlanningError, match="no duration"):
        planner.plan("a meeting of no length")


# --------------------------------------------------------------------------- #
# Saying when, in the ways people actually say it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("next week", dt.date(2026, 11, 2)),
        ("on Thursday", dt.date(2026, 10, 29)),
        ("next Thursday", dt.date(2026, 11, 5)),
        ("tomorrow", dt.date(2026, 10, 28)),
    ],
)
def test_a_meeting_can_be_asked_for_in_several_ways(backend, phrase, expected):
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(meeting=MeetingIntent(attendee_names=["Anu Prakash"], when_phrase=phrase))
        ),
        today=TUE,
    )
    plan = planner.plan(f"meet Anu {phrase}")
    assert plan.window_start == expected


def test_an_unresolvable_time_still_asks(backend):
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(
                meeting=MeetingIntent(attendee_names=["Anu Prakash"], when_phrase="after the audit")
            )
        ),
        today=TUE,
    )
    plan = planner.plan("meet Anu after the audit")
    assert plan.needs_input


# --------------------------------------------------------------------------- #
# Email shapes beyond "send this file"
# --------------------------------------------------------------------------- #


def test_an_email_with_no_attachment(backend):
    """"Tell Anu I'll be late" - a message, not a delivery."""
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(
                email=EmailIntent(
                    recipient_names=["Anu Prakash"],
                    subject="Running late",
                    body="I'll be late for the review.",
                )
            )
        ),
        today=TUE,
    )
    plan = planner.plan("tell Anu I'll be late")
    draft = next(s for s in plan.steps if s.action.tool is ToolName.DRAFT_EMAIL)
    assert draft.action.args["attachments"] == []
    assert draft.action.args["body"] == "I'll be late for the review."


def test_an_email_to_several_people(backend):
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(
                email=EmailIntent(
                    recipient_names=["Rahul Menon", "Anu Prakash"], attachment_query="Q3 report"
                )
            )
        ),
        today=TUE,
    )
    plan = planner.plan("send the Q3 report to both", ANSWERED)
    draft = next(s for s in plan.steps if s.action.tool is ToolName.DRAFT_EMAIL)
    assert draft.action.args["to"] == [RAHUL, ANU]


def test_an_email_and_a_meeting_together_still_gate_both(backend):
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    plan = planner.plan(
        "Email Rahul Menon the Q3 report and set a 30-minute review with him and Anu next week.",
        ANSWERED,
    )
    gated = [s.action.tool for s in plan.steps if s.action.has_side_effect]
    assert gated == [ToolName.SEND_EMAIL, ToolName.CREATE_EVENT]
