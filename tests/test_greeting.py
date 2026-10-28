"""The covering note RELAY writes when the goal does not supply one.

This exists because a real send went out addressed to "Hi Nahslll969," - the
left half of an email address, title-cased. The lookup had the person's actual
name the whole time and the resolver discarded it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import ANU, RAHUL
from relay.planner import Choices, Planner, RegexIntentReader
from relay.schemas import ToolName
from relay.tools import MockBackend

TUE = dt.date(2026, 10, 27)
ANSWERED = Choices(files={"Q3 report": "f_q3_final"})


@pytest.fixture
def planner(backend):
    return Planner(backend=backend, reader=RegexIntentReader(), today=TUE)


def _body(planner, goal=None):
    plan = planner.plan(goal or "Email Rahul Menon the Q3 report.", ANSWERED)
    draft = next(s for s in plan.steps if s.action.tool is ToolName.DRAFT_EMAIL)
    return draft.action.args["body"]


# --------------------------------------------------------------------------- #
# The name comes from the contact, never from the address
# --------------------------------------------------------------------------- #


def test_the_greeting_uses_the_contacts_real_name(planner):
    assert _body(planner).startswith("Hi Rahul,")


def test_the_greeting_is_not_derived_from_the_address():
    """The original bug, with the address that produced it."""
    backend = MockBackend()
    backend._data["contacts"] = [
        {"name": "Guppy Bell", "email": "bell9821@example.com", "note": "me"}
    ]
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    body = _body(planner, "Email Guppy Bell the Q3 report.")

    assert body.startswith("Hi Guppy,")
    assert "Bell9821" not in body


def test_a_name_with_digits_falls_back_to_neutral(planner):
    """"Hi X7" is worse than greeting nobody by name."""
    planner._resolved_names = {RAHUL: "X7 Smith"}
    assert planner._greeting([RAHUL]) == "Hi there"


def test_a_single_letter_name_falls_back(planner):
    planner._resolved_names = {RAHUL: "R"}
    assert planner._greeting([RAHUL]) == "Hi there"


def test_an_unknown_address_falls_back(planner):
    assert planner._greeting(["stranger@example.com"]) == "Hi there"


def test_no_recipients_falls_back(planner):
    assert planner._greeting([]) == "Hi there"


# --------------------------------------------------------------------------- #
# Several recipients
# --------------------------------------------------------------------------- #


def test_two_recipients_are_both_greeted(planner):
    planner._resolved_names = {RAHUL: "Rahul Menon", ANU: "Anu Prakash"}
    assert planner._greeting([RAHUL, ANU]) == "Hi Rahul and Anu"


def test_three_recipients_read_as_a_list(planner):
    third = "priya.nair@relay-test.example"
    planner._resolved_names = {RAHUL: "Rahul Menon", ANU: "Anu Prakash", third: "Priya Nair"}
    assert planner._greeting([RAHUL, ANU, third]) == "Hi Rahul, Anu and Priya"


def test_one_missing_name_makes_everyone_neutral(planner):
    """"Hi Rahul and there" is worse than greeting nobody by name."""
    planner._resolved_names = {RAHUL: "Rahul Menon"}
    assert planner._greeting([RAHUL, "stranger@example.com"]) == "Hi there"


# --------------------------------------------------------------------------- #
# Naming the attachment
# --------------------------------------------------------------------------- #


def test_the_attachment_is_named_not_called_the_report(planner):
    body = _body(planner)
    assert "I have attached the Q3 report." in body
    # The old wording assumed every attachment was a report.
    assert "attached the report" not in body


def test_an_article_is_added_only_when_missing(planner):
    assert "attached the Q3 report." in planner._default_body([RAHUL], ["f"], "Q3 report")
    assert "attached the Q3 report." in planner._default_body([RAHUL], ["f"], "the Q3 report")
    assert "attached my slides." in planner._default_body([RAHUL], ["f"], "my slides")


def test_an_email_with_no_attachment_says_nothing_about_one(planner):
    body = planner._default_body([RAHUL], [], None)
    assert "attached" not in body


def test_an_unnamed_attachment_is_still_described(planner):
    assert "attached the file." in planner._default_body([RAHUL], ["f"], None)


# --------------------------------------------------------------------------- #
# The goal's own words always win
# --------------------------------------------------------------------------- #


def test_a_body_from_the_goal_is_never_overwritten(backend):
    """"Tell Anu I'll be late" supplies its own body; no greeting is invented."""
    from relay.planner import EmailIntent, GoalIntent

    class FixedReader:
        def read(self, goal):
            return GoalIntent(
                email=EmailIntent(recipient_names=["Anu Prakash"], body="I'll be late.")
            )

    planner = Planner(backend=backend, reader=FixedReader(), today=TUE)
    plan = planner.plan("tell Anu")
    draft = next(s for s in plan.steps if s.action.tool is ToolName.DRAFT_EMAIL)
    assert draft.action.args["body"] == "I'll be late."
