"""An invitation is an email, so one request must not go out twice.

"Email to dhilshad for a meeting tomorrow at 3pm" planned both halves: a
send_email saying "I would like to schedule a meeting with you for tomorrow
at 3pm", and a create_event, which emails an .ics invitation for 3pm to the
same person. He received the same request twice.

The model instruction now says that booking a meeting already emails an
invitation. That alone is not enough: the same goal read as email-only on one
call and as email-plus-meeting on another. A prompt cannot be relied on to
hold an invariant, so the planner checks - and these tests drive the reader
directly, with the model's answer fixed, precisely to test the check rather
than the prompt.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import RAHUL
from relay.planner import Choices, EmailIntent, GoalIntent, MeetingIntent, Planner
from relay.schemas import ToolName

TUE = dt.date(2026, 10, 27)
FILES = {"Q3 report": "f_q3_final"}


class FixedReader:
    def __init__(self, intent):
        self._intent = intent

    def read(self, goal):
        return self._intent


def _plan(backend, intent, goal="Email Rahul Menon about a meeting.", choices=None):
    planner = Planner(backend=backend, reader=FixedReader(intent), today=TUE)
    return planner.plan(goal, choices or Choices(contacts={"Rahul Menon": RAHUL}))


def tools(plan):
    return [s.action.tool for s in plan.steps]


BOTH_NO_ATTACHMENT = GoalIntent(
    email=EmailIntent(
        recipient_names=["Rahul Menon"],
        subject="Meeting request",
        body="I would like to schedule a meeting with you for tomorrow at 3pm.",
    ),
    meeting=MeetingIntent(
        attendee_names=["Rahul Menon"], when_phrase="tomorrow", at_time="3pm"
    ),
)


# --------------------------------------------------------------------------- #
# The duplicate
# --------------------------------------------------------------------------- #


def test_a_meeting_request_is_not_also_emailed(backend):
    plan = _plan(backend, BOTH_NO_ATTACHMENT)
    assert ToolName.SEND_EMAIL not in tools(plan)
    assert ToolName.CREATE_EVENT in tools(plan)


def test_only_one_message_leaves(backend):
    """The property that actually matters to the recipient."""
    plan = _plan(backend, BOTH_NO_ATTACHMENT)
    sending = [t for t in tools(plan) if t in {ToolName.SEND_EMAIL, ToolName.CREATE_EVENT}]
    assert len(sending) == 1


def test_the_plan_says_why_there_is_no_email(backend):
    """Dropping a step the user can see they asked for, without a word, would
    look like RELAY losing half the goal."""
    plan = _plan(backend, BOTH_NO_ATTACHMENT)
    notes = [
        s.action.args["text"]
        for s in plan.steps
        if s.action.tool is ToolName.NOTE
    ]
    assert any("invitation" in n for n in notes), notes


# --------------------------------------------------------------------------- #
# What must NOT be folded
# --------------------------------------------------------------------------- #


def test_an_email_carrying_a_document_still_goes(backend):
    """"Email Rahul the Q3 report and book a review" is genuinely two things:
    the invitation cannot carry the report."""
    intent = GoalIntent(
        email=EmailIntent(recipient_names=["Rahul Menon"], attachment_query="Q3 report"),
        meeting=MeetingIntent(attendee_names=["Rahul Menon"], when_phrase="next week"),
    )
    plan = _plan(
        backend,
        intent,
        choices=Choices(contacts={"Rahul Menon": RAHUL}, files=FILES),
    )
    assert ToolName.SEND_EMAIL in tools(plan)
    assert ToolName.CREATE_EVENT in tools(plan)


def test_an_email_to_someone_not_in_the_meeting_still_goes(backend):
    """Telling Anu about a meeting with Rahul is not a duplicate of anything:
    she gets no invitation."""
    intent = GoalIntent(
        email=EmailIntent(recipient_names=["Anu Prakash"], subject="FYI"),
        meeting=MeetingIntent(attendee_names=["Rahul Menon"], when_phrase="next week"),
    )
    plan = _plan(
        backend,
        intent,
        choices=Choices(contacts={"Anu Prakash": "anu.prakash@relay-test.example",
                                  "Rahul Menon": RAHUL}),
    )
    assert ToolName.SEND_EMAIL in tools(plan)
    assert ToolName.CREATE_EVENT in tools(plan)


def test_an_email_with_no_meeting_is_untouched(backend):
    intent = GoalIntent(
        email=EmailIntent(recipient_names=["Rahul Menon"], subject="Review")
    )
    plan = _plan(backend, intent)
    assert ToolName.SEND_EMAIL in tools(plan)


def test_a_meeting_with_no_email_is_untouched(backend):
    intent = GoalIntent(
        meeting=MeetingIntent(attendee_names=["Rahul Menon"], when_phrase="next week")
    )
    plan = _plan(backend, intent)
    assert ToolName.CREATE_EVENT in tools(plan)
    assert ToolName.SEND_EMAIL not in tools(plan)


@pytest.mark.parametrize("extra", ["Anu Prakash"])
def test_a_wider_email_audience_is_not_folded(backend, extra):
    """The email goes to more people than the meeting does, so folding it
    would silently drop everyone who was not invited."""
    intent = GoalIntent(
        email=EmailIntent(recipient_names=["Rahul Menon", extra], subject="Plan"),
        meeting=MeetingIntent(attendee_names=["Rahul Menon"], when_phrase="next week"),
    )
    plan = _plan(
        backend,
        intent,
        choices=Choices(contacts={"Rahul Menon": RAHUL,
                                  extra: "anu.prakash@relay-test.example"}),
    )
    assert ToolName.SEND_EMAIL in tools(plan)
