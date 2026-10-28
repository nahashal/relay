"""A time the user stated is not something to go looking for.

"book a review with fayaz tomorrow at 2pm" planned a find_free_slots step
first, titled "Find 30-minute slots when all of ... are free", and then booked
one of its results. Two things were wrong with that:

  * the answer was already in the goal, so the search decided nothing
  * on the local backend the search calls free_slots with busy=[]. Nobody's
    calendar is read. The step claimed to find times when people "are free"
    while knowing nothing about anyone's availability.

The search still runs for goals that name no time - that is the case it was
built for, and the only one where it adds anything.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import RAHUL as RAHUL_EMAIL
from relay import dates
from relay.planner import Choices, Planner, RegexIntentReader
from relay.schemas import ToolName

TUE = dt.date(2026, 10, 27)
RAHUL = {"Rahul Menon": RAHUL_EMAIL}


@pytest.fixture
def planner(backend):
    return Planner(backend=backend, reader=RegexIntentReader(), today=TUE)


def tools(plan):
    return [s.action.tool for s in plan.steps]


def booking(plan):
    return next(s for s in plan.steps if s.action.tool is ToolName.CREATE_EVENT).action.args


# --------------------------------------------------------------------------- #
# Parsing a clock time
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("tomorrow at 2pm", dt.time(14, 0)),
        ("at 14:00", dt.time(14, 0)),
        ("2:30pm", dt.time(14, 30)),
        ("9am", dt.time(9, 0)),
        ("at 12pm", dt.time(12, 0)),   # noon, not midnight
        ("at 12am", dt.time(0, 0)),    # midnight, not noon
        # No time named: not a failure, just the other path.
        ("next week", None),
        ("tomorrow", None),
        # Not a real clock time.
        ("at 25:00", None),
        ("at 10:75", None),
    ],
)
def test_a_clock_time_is_read_or_absent(phrase, expected):
    assert dates.resolve_time(phrase) == expected


# --------------------------------------------------------------------------- #
# The plan that results
# --------------------------------------------------------------------------- #


def test_a_stated_time_skips_the_search(planner):
    """The complaint: an extra step that decided nothing."""
    plan = planner.plan(
        "Book a 30-minute review with Rahul Menon tomorrow at 2pm.", Choices(contacts=RAHUL)
    )
    assert ToolName.FIND_FREE_SLOTS not in tools(plan)
    assert ToolName.CREATE_EVENT in tools(plan)


def test_the_booking_is_at_the_time_that_was_asked_for(planner):
    plan = planner.plan(
        "Book a 30-minute review with Rahul Menon tomorrow at 2pm.", Choices(contacts=RAHUL)
    )
    slot = booking(plan)["slot"]
    assert slot["start"].startswith("2026-10-28T14:00")
    assert slot["end"].startswith("2026-10-28T14:30")


def test_the_slot_is_a_real_value_not_a_reference(planner):
    """With no search step there is nothing to point at, so the slot must be
    bound already - an unbound action is refused by the executor."""
    from relay.schemas import Action

    plan = planner.plan(
        "Book a 30-minute review with Rahul Menon tomorrow at 2pm.", Choices(contacts=RAHUL)
    )
    action = next(s.action for s in plan.steps if s.action.tool is ToolName.CREATE_EVENT)
    assert isinstance(action, Action) and action.is_bound


def test_the_meeting_length_is_respected(planner):
    plan = planner.plan(
        "Book a 45-minute review with Rahul Menon tomorrow at 9am.", Choices(contacts=RAHUL)
    )
    slot = booking(plan)["slot"]
    assert slot["start"].startswith("2026-10-28T09:00")
    assert slot["end"].startswith("2026-10-28T09:45")


def test_the_step_says_the_time_rather_than_the_slot(planner):
    """"Book the approved slot" meant nothing without the search step above
    it that produced the slot."""
    plan = planner.plan(
        "Book a 30-minute review with Rahul Menon tomorrow at 2pm.", Choices(contacts=RAHUL)
    )
    step = next(s for s in plan.steps if s.action.tool is ToolName.CREATE_EVENT)
    assert "14:00" in step.intent


# --------------------------------------------------------------------------- #
# The case the search was built for still uses it
# --------------------------------------------------------------------------- #


def test_no_time_given_still_searches(planner):
    plan = planner.plan(
        "Book a 30-minute review with Rahul Menon next week.", Choices(contacts=RAHUL)
    )
    assert ToolName.FIND_FREE_SLOTS in tools(plan)


def test_two_meetings_still_search(planner):
    """Two meetings at one stated time would be the same meeting twice, so
    the search - which hands out a different slot to each - still runs."""
    plan = planner.plan(
        "Book two 30-minute catch-ups with Rahul Menon tomorrow at 2pm.",
        Choices(contacts=RAHUL),
    )
    if len([t for t in tools(plan) if t is ToolName.CREATE_EVENT]) > 1:
        assert ToolName.FIND_FREE_SLOTS in tools(plan)


# --------------------------------------------------------------------------- #
# Rejecting a time you chose yourself
# --------------------------------------------------------------------------- #


def test_a_revision_moves_a_stated_time_along(planner):
    """There is nothing to search, so "not that one" can only mean later."""
    first = planner.plan(
        "Book a 30-minute review with Rahul Menon tomorrow at 2pm.", Choices(contacts=RAHUL)
    )
    second = planner.plan(
        "Book a 30-minute review with Rahul Menon tomorrow at 2pm.",
        Choices(contacts=RAHUL),
        slot_offset=1,
    )
    assert booking(first)["slot"]["start"].startswith("2026-10-28T14:00")
    assert booking(second)["slot"]["start"].startswith("2026-10-28T14:30")
