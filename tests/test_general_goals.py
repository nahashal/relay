"""Planning is general; acting is not.

RELAY breaks ANY goal into steps. The ones it has a tool for it performs, gated
as always. The rest become a checklist it reports and does not pretend to have
done. A goal with no tool step at all is still a good goal.

What does NOT become a checklist: an action RELAY must refuse rather than
approximate. "Delete my emails" is not a manual step, it is a refusal.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import RAHUL
from relay.loop import Awaiting, new_loop
from relay.planner import (
    Choices,
    EmailIntent,
    GoalIntent,
    Planner,
    PlanningError,
    RegexIntentReader,
)
from relay.schemas import Decision, StopState, ToolName

TUE = dt.date(2026, 10, 27)
ANSWERED = Choices(files={"Q3 report": "f_q3_final"})


class FixedReader:
    def __init__(self, intent: GoalIntent) -> None:
        self._intent = intent

    def read(self, goal: str) -> GoalIntent:
        return self._intent


@pytest.fixture
def planner(backend):
    return Planner(backend=backend, reader=RegexIntentReader(), today=TUE)


def _tools(plan):
    return [s.action.tool for s in plan.steps]


# --------------------------------------------------------------------------- #
# A goal with no tool step is still a plan
# --------------------------------------------------------------------------- #


def test_a_goal_with_no_tools_plans_as_notes(backend):
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(
                manual_steps=[
                    "List the subjects that need study time.",
                    "Block 7-8pm on weekdays for revision.",
                    "Review the timetable each Friday.",
                ]
            )
        ),
        today=TUE,
    )
    plan = planner.plan("plan a weekly study timetable")
    assert _tools(plan) == [ToolName.NOTE, ToolName.NOTE, ToolName.NOTE, ToolName.FINISH]


def test_notes_need_no_approval(backend):
    """Nothing reaches the outside world, so there is nothing to gate."""
    planner = Planner(
        backend=backend, reader=FixedReader(GoalIntent(manual_steps=["Tidy the desk."])), today=TUE
    )
    plan = planner.plan("tidy up")
    assert not any(s.action.has_side_effect for s in plan.steps)


def test_such_a_run_ends_done_with_the_checklist(backend, audit):
    planner = Planner(
        backend=backend,
        reader=FixedReader(GoalIntent(manual_steps=["Book a room.", "Print the agenda."])),
        today=TUE,
    )
    loop = new_loop(planner, audit, run_id="run_notes")
    status = loop.start("prepare for the review")

    assert status.finished and status.stop_state is StopState.DONE
    assert loop.checklist() == ["Book a room.", "Print the agenda."]
    assert "Over to you:" in status.summary
    assert "1. Book a room." in status.summary


def test_the_checklist_reaches_the_status_for_the_ui(backend, audit):
    planner = Planner(
        backend=backend, reader=FixedReader(GoalIntent(manual_steps=["Do the thing."])), today=TUE
    )
    status = new_loop(planner, audit, run_id="run_ui").start("x")
    assert status.checklist == ["Do the thing."]


# --------------------------------------------------------------------------- #
# A goal with both: act on what it can, list the rest
# --------------------------------------------------------------------------- #


@pytest.fixture
def mixed(backend, audit):
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(
                email=EmailIntent(recipient_names=["Rahul Menon"], attachment_query="Q3 report"),
                manual_steps=["Draft talking points before the meeting."],
            )
        ),
        today=TUE,
    )
    return new_loop(planner, audit, run_id="run_mixed"), backend


def test_tool_steps_come_before_the_checklist(mixed):
    loop, _ = mixed
    loop.choices = ANSWERED
    loop.start("email Rahul and prepare notes")
    assert _tools(loop.plan) == [
        ToolName.DRAFT_EMAIL,
        ToolName.SEND_EMAIL,
        ToolName.NOTE,
        ToolName.FINISH,
    ]


def test_the_report_separates_what_was_done_from_what_was_not(mixed):
    """An agent must not take credit for work nobody did."""
    loop, backend = mixed
    loop.choices = ANSWERED
    status = loop.start("email Rahul and prepare notes")
    while status.awaiting is Awaiting.APPROVAL:
        status = loop.decide(status.card.step_number, Decision(verdict="approve"))

    assert status.stop_state is StopState.DONE
    assert "Done: the email was sent." in status.summary
    assert "Over to you:" in status.summary
    assert "Draft talking points" in status.summary
    assert backend.sent_count == 1


def test_the_side_effect_is_still_gated(mixed):
    loop, backend = mixed
    loop.choices = ANSWERED
    status = loop.start("email Rahul and prepare notes")
    # It stops for approval before sending, exactly as before.
    assert status.awaiting is Awaiting.APPROVAL
    assert status.card.action.tool is ToolName.SEND_EMAIL
    assert backend.sent_count == 0


def test_rejecting_still_stops_the_whole_run(mixed):
    loop, backend = mixed
    loop.choices = ANSWERED
    status = loop.start("email Rahul and prepare notes")
    status = loop.decide(status.card.step_number, Decision(verdict="reject"))
    assert status.stop_state is StopState.REJECTED
    assert backend.sent_count == 0


# --------------------------------------------------------------------------- #
# What must NOT become a checklist
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "goal",
    [
        "Delete all my emails from Rahul.",
        "Forward the Q3 report to Anu.",
        "Cancel the review with Rahul Menon.",
        "Unsubscribe me from the newsletter.",
    ],
)
def test_a_destructive_action_is_refused_not_listed(planner, goal):
    """The distinction the whole change turns on: a goal asking RELAY to DO
    something it must not approximate is refused. A goal asking it to PLAN
    something becomes a checklist."""
    with pytest.raises(PlanningError, match="cannot do that"):
        planner.plan(goal, ANSWERED)


def test_unsupported_wins_over_manual_steps(backend):
    """If a reader returns both, the refusal takes precedence."""
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(unsupported=["delete my mail"], manual_steps=["Tidy the inbox."])
        ),
        today=TUE,
    )
    with pytest.raises(PlanningError, match="cannot do that"):
        planner.plan("delete my mail and tidy up")


def test_a_goal_with_nothing_in_it_at_all_still_fails(backend):
    planner = Planner(backend=backend, reader=FixedReader(GoalIntent()), today=TUE)
    with pytest.raises(PlanningError, match="could not work out any steps"):
        planner.plan("")


# --------------------------------------------------------------------------- #
# Budgets still apply
# --------------------------------------------------------------------------- #


def test_notes_cost_a_tool_call_so_the_budget_still_bounds_a_run(backend, audit):
    """A checklist is not free: a 50-step plan should hit the budget, not run on."""
    planner = Planner(
        backend=backend,
        reader=FixedReader(GoalIntent(manual_steps=[f"Step {i}" for i in range(30)])),
        today=TUE,
    )
    loop = new_loop(planner, audit, run_id="run_many", max_tool_calls=5)
    status = loop.start("a very long goal")
    assert status.stop_state is StopState.BUDGET_HIT
