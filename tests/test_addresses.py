"""Where a recipient's address is allowed to come from.

Three sources, and they are not equally trusted:

  a contact lookup   - always fine, it is the user's own directory
  the user, directly - fine: typed in the goal, or given as an answer
  the model          - never

The last two both arrive as a plain string, so they are told apart by evidence:
an address-shaped name is accepted only if it literally appears in the goal the
user typed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import RAHUL
from relay.planner import (
    Choices,
    EmailIntent,
    GoalIntent,
    Planner,
    RegexIntentReader,
    looks_like_email,
)
from relay.schemas import ToolName

TUE = dt.date(2026, 10, 27)
FILES = {"Q3 report": "f_q3_final"}


@pytest.fixture
def planner(backend):
    return Planner(backend=backend, reader=RegexIntentReader(), today=TUE)


def _recipients(plan):
    draft = next(s for s in plan.steps if s.action.tool is ToolName.DRAFT_EMAIL)
    return draft.action.args["to"]


class FixedReader:
    def __init__(self, intent):
        self._intent = intent

    def read(self, goal):
        return self._intent


# --------------------------------------------------------------------------- #
# The shape test
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["a@b.co", "rahul.menon@relay-test.example", " x@y.z "])
def test_addresses_are_recognised(value):
    assert looks_like_email(value)


@pytest.mark.parametrize("value", ["Rahul Menon", "rahul@", "@example.com", "rahul at x.com", ""])
def test_non_addresses_are_not(value):
    assert not looks_like_email(value)


# --------------------------------------------------------------------------- #
# The user typing an address in the goal
# --------------------------------------------------------------------------- #


def test_an_address_in_the_goal_is_used_directly(planner):
    """Pasting an address is a natural thing to do; it used to fail to parse."""
    plan = planner.plan(
        "Email rahul.menon@relay-test.example the Q3 report.", Choices(files=FILES)
    )
    assert not plan.needs_input
    assert _recipients(plan) == [RAHUL]


def test_an_address_in_the_goal_needs_no_contact_record(backend):
    """The whole point: it works for someone not in the address book."""
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    plan = planner.plan("Email stranger@elsewhere.example the Q3 report.", Choices(files=FILES))
    assert _recipients(plan) == ["stranger@elsewhere.example"]


def test_an_address_not_in_the_goal_is_refused(backend):
    """The guard: a reader that invents an address must not get it into a plan."""
    planner = Planner(
        backend=backend,
        reader=FixedReader(GoalIntent(email=EmailIntent(recipient_names=["attacker@evil.example"]))),
        today=TUE,
    )
    plan = planner.plan("Email Rahul Menon the Q3 report.", Choices(files=FILES))
    assert plan.needs_input
    assert "does not appear in your goal" in plan.open_questions[0]


def test_the_check_is_case_insensitive(backend):
    """Readers routinely lowercase an address; that is not evidence of invention."""
    planner = Planner(
        backend=backend,
        reader=FixedReader(
            GoalIntent(email=EmailIntent(recipient_names=["rahul.menon@relay-test.example"]))
        ),
        today=TUE,
    )
    plan = planner.plan("Email Rahul.Menon@Relay-Test.Example the report.", Choices(files=FILES))
    assert not plan.needs_input


# --------------------------------------------------------------------------- #
# The user answering "what is their address?"
# --------------------------------------------------------------------------- #


def test_answering_with_an_address_resolves_the_question(planner):
    """RELAY asked this question for months and then ignored the answer."""
    first = planner.plan("Email Zoltan the Q3 report.", Choices(files=FILES))
    assert "What is their address?" in first.open_questions[0]

    answered = planner.plan(
        "Email Zoltan the Q3 report.",
        Choices(files=FILES, contacts={"Zoltan": "zoltan@elsewhere.example"}),
    )
    assert not answered.needs_input
    assert _recipients(answered) == ["zoltan@elsewhere.example"]


def test_the_same_question_is_not_asked_twice(planner):
    """The old behaviour looped until the replan budget stopped the run."""
    answered = planner.plan(
        "Email Zoltan the Q3 report.",
        Choices(files=FILES, contacts={"Zoltan": "zoltan@elsewhere.example"}),
    )
    assert "What is their address?" not in str(answered.open_questions)


def test_a_supplied_address_is_greeted_by_the_name_the_user_used(planner):
    plan = planner.plan(
        "Email Zoltan the Q3 report.",
        Choices(files=FILES, contacts={"Zoltan": "zoltan@elsewhere.example"}),
    )
    draft = next(s for s in plan.steps if s.action.tool is ToolName.DRAFT_EMAIL)
    assert draft.action.args["body"].startswith("Hi Zoltan,")


def test_an_answer_that_is_not_an_address_is_rejected(planner):
    plan = planner.plan(
        "Email Zoltan the Q3 report.",
        Choices(files=FILES, contacts={"Zoltan": "my mate zoltan"}),
    )
    assert plan.needs_input
    assert "does not look like an email address" in plan.open_questions[0]


def test_answering_a_known_name_must_still_pick_a_candidate(planner):
    """When the directory DOES have candidates, an answer must be one of them.

    Different rule from the no-matches case on purpose: if RELAY found two
    Rahuls, an answer naming a third address is a mistake, not new information.
    """
    plan = planner.plan(
        "Email Rahul the Q3 report.",
        Choices(files=FILES, contacts={"Rahul": "someone.else@elsewhere.example"}),
    )
    assert plan.needs_input
    assert "is not one of the contacts" in plan.open_questions[0]


def test_a_supplied_address_counts_as_resolved(planner):
    """It must satisfy the tripwire, or the plan fails after being answered."""
    plan = planner.plan(
        "Email Zoltan the Q3 report.",
        Choices(files=FILES, contacts={"Zoltan": "zoltan@elsewhere.example"}),
    )
    assert "zoltan@elsewhere.example" in planner._resolved_emails
    planner._assert_resolved_only(plan)  # does not raise
