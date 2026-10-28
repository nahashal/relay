"""Addresses the user typed, remembered instead of asked for twice.

RELAY already trusted a user-typed address - "it is their contact, not ours" -
and then discarded it when the run ended, so the second email to the same
person asked the same question. That is what this stops.

The rule being protected is the one `test_addresses.py` draws: RELAY learns
what the USER typed, never what the model produced. Learning is not acting -
nothing is sent and nothing leaves the machine - so it needs no approval, and
it is not contingent on the run finishing.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from relay.brevo import BrevoClient
from relay.planner import (
    Choices,
    EmailIntent,
    GoalIntent,
    Planner,
    RegexIntentReader,
    name_for_address,
)
from relay.schemas import ToolName
from relay.tools import MockBackend
from relay.tools.local_backend import LocalBackend

TUE = dt.date(2026, 10, 27)
FILES = {"Q3 report": "f_q3_final"}
FAYAZ = "fayastm03@gmail.com"


class FixedReader:
    """Says exactly what it was told to, so the regex reader's limits do not
    decide what these tests can express."""

    def __init__(self, intent):
        self._intent = intent

    def read(self, goal):
        return self._intent


def _emailing(*names: str) -> GoalIntent:
    return GoalIntent(
        email=EmailIntent(recipient_names=list(names), attachment_query="Q3 report")
    )


def _plan_with(backend, intent, goal, choices=None):
    planner = Planner(backend=backend, reader=FixedReader(intent), today=TUE)
    return planner.plan(goal, choices or Choices(files=FILES))


def _draft(plan):
    return next(s for s in plan.steps if s.action.tool is ToolName.DRAFT_EMAIL).action.args


# --------------------------------------------------------------------------- #
# Reading the name out of the goal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "goal, address, expected",
    [
        (f"send hi to fayaz {FAYAZ}", FAYAZ, "fayaz"),
        ("email Rahul Menon rahul@example.com the report", "rahul@example.com", "Rahul Menon"),
        (f"send hi to fayaz <{FAYAZ}>", FAYAZ, "fayaz"),
        # Nobody is named, so nobody may be invented.
        (f"send this to {FAYAZ}", FAYAZ, None),
        (f"email {FAYAZ}", FAYAZ, None),
        (f"cc {FAYAZ}", FAYAZ, None),
        # The address is the whole goal: there is nothing in front of it.
        (FAYAZ, FAYAZ, None),
    ],
)
def test_the_name_comes_from_what_the_user_wrote(goal, address, expected):
    assert name_for_address(goal, address) == expected


def test_an_address_absent_from_the_goal_has_no_name():
    """Guards the invariant: a model-produced address cannot smuggle a name."""
    assert name_for_address("send hi to fayaz", "someone@invented.example") is None


# --------------------------------------------------------------------------- #
# Learning from an address typed in the goal
# --------------------------------------------------------------------------- #


def test_an_address_typed_in_a_goal_is_remembered(backend):
    _plan_with(backend, _emailing(FAYAZ), f"Email fayaz {FAYAZ} the Q3 report.")
    assert [c["email"] for c in backend.find_contact("fayaz")] == [FAYAZ]


def test_the_second_goal_needs_no_address(backend):
    """The whole point: asked once, not every time."""
    _plan_with(backend, _emailing(FAYAZ), f"Email fayaz {FAYAZ} the Q3 report.")
    plan = _plan_with(backend, _emailing("fayaz"), "Email fayaz the Q3 report.")

    assert not plan.open_questions
    assert _draft(plan)["to"] == [FAYAZ]


def test_the_learned_name_is_used_to_greet(backend):
    """Without a name the body opened "Hi Fayastm03", taken from the address."""
    plan = _plan_with(backend, _emailing(FAYAZ), f"Email fayaz {FAYAZ} the Q3 report.")
    assert "Fayastm03" not in _draft(plan)["body"]


def test_an_unnamed_address_is_not_remembered(backend):
    """"Email <address>" names nobody, so nobody is learned."""
    _plan_with(backend, _emailing(FAYAZ), f"Email {FAYAZ} the Q3 report.")
    assert backend.find_contact("fayaz") == []


def test_an_address_the_model_invented_is_never_learned(backend):
    """The address is not in the goal, so the reader produced it. It must be
    questioned - storing it would launder a hallucination into a contact
    indistinguishable from the user's own."""
    invented = "fayaz@invented.example"
    plan = _plan_with(backend, _emailing(invented), "Email Fayaz the Q3 report.")

    assert plan.open_questions
    assert [c for c in backend.find_contact("") if c["email"] == invented] == []


# --------------------------------------------------------------------------- #
# Learning from the answer to "what is their address?"
# --------------------------------------------------------------------------- #


def test_an_answered_address_is_remembered(backend):
    """This is the question the user got tired of answering twice."""
    _plan_with(
        backend,
        _emailing("Fayaz"),
        "Email Fayaz the Q3 report.",
        Choices(contacts={"Fayaz": FAYAZ}, files=FILES),
    )
    assert [c["email"] for c in backend.find_contact("Fayaz")] == [FAYAZ]


def test_a_rejected_run_still_learns(backend):
    """Learning is not contingent on the send: "fayaz is <address>" is true
    whether or not the email goes, and a rejected draft is no reason to
    forget it."""
    _plan_with(backend, _emailing(FAYAZ), f"Email fayaz {FAYAZ} the Q3 report.")
    assert backend.find_contact("fayaz")
    assert backend.sent_count == 0


# --------------------------------------------------------------------------- #
# The file the local backend writes
# --------------------------------------------------------------------------- #


class _FakeTransport:
    def __call__(self, method, url, headers, body):
        return 201, {"messageId": "<x@relay>"}


@pytest.fixture
def local(tmp_path):
    def build(rows_or_text):
        path = tmp_path / "contacts.json"
        path.write_text(
            rows_or_text if isinstance(rows_or_text, str) else json.dumps(rows_or_text),
            encoding="utf-8",
        )
        return LocalBackend(
            me="me@example.com",
            documents=tmp_path,
            contacts=path,
            brevo=BrevoClient(
                api_key="test-key", sender_email="me@example.com", transport=_FakeTransport()
            ),
        ), path

    return build


def test_the_local_backend_writes_the_contact(local):
    backend, path = local([{"name": "Anu", "email": "anu@x.com", "note": "Ops"}])

    assert backend.remember_contact("fayaz", FAYAZ) is True
    assert [r["email"] for r in json.loads(path.read_text(encoding="utf-8"))] == [
        "anu@x.com",
        FAYAZ,
    ]


def test_a_known_address_is_not_duplicated(local):
    backend, path = local([{"name": "Anu", "email": "anu@x.com", "note": "Ops"}])

    assert backend.remember_contact("Anu Prakash", "ANU@x.com") is False
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 1


def test_an_unreadable_contacts_file_is_never_clobbered(local):
    """Losing the user's contacts to a stray comma would be a far worse bug
    than asking for an address twice."""
    backend, path = local("{ this is not json")

    assert backend.remember_contact("fayaz", FAYAZ) is False
    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_a_learned_contact_is_findable_again(local):
    """Writing it is only half the job; find_contact has to see it."""
    backend, _ = local([])
    backend.remember_contact("fayaz", FAYAZ)
    assert [c["email"] for c in backend.find_contact("fayaz")] == [FAYAZ]


def test_the_mock_and_local_backends_agree(local):
    """One protocol, the same answers - what the three backends exist to keep."""
    local_backend, _ = local([])

    for backend in (local_backend, MockBackend()):
        assert backend.remember_contact("fayaz", FAYAZ) is True
        assert backend.remember_contact("fayaz", FAYAZ) is False
        assert backend.remember_contact("", FAYAZ) is False
        assert backend.remember_contact("fayaz", "") is False


def test_learning_never_reaches_the_gate(backend):
    """It must not be a side effect: nothing leaves the machine."""
    from relay.schemas import SIDE_EFFECT_TOOLS

    assert SIDE_EFFECT_TOOLS == {ToolName.SEND_EMAIL, ToolName.CREATE_EVENT}
    backend.remember_contact("fayaz", FAYAZ)
    assert backend.sent_count == 0 and backend.event_count == 0


# --------------------------------------------------------------------------- #
# The branch that actually fires: the model reports a NAME and drops the
# address, so the goal is the only place the address still exists.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "goal, name, expected",
    [
        (f"send a hello to fayaz {FAYAZ}", "fayaz", FAYAZ),
        (f"send a hello to fayaz <{FAYAZ}>", "fayaz", FAYAZ),
        (f"email Rahul Menon (rahul@x.co) the report", "Rahul Menon", "rahul@x.co"),
        # Someone else's address, further along the sentence. Adjacency is the
        # whole safety property here: attaching this to "fayaz" would send a
        # stranger's mail to the wrong person.
        (f"send fayaz the report and cc anu {FAYAZ}", "fayaz", None),
        ("send fayaz the report", "fayaz", None),
        # The name is not in the goal at all, so there is nothing to sit next to.
        (f"send a hello to {FAYAZ}", "fayaz", None),
    ],
)
def test_an_address_beside_the_name_is_found(goal, name, expected):
    from relay.planner import address_for_name

    assert address_for_name(goal, name) == expected


def test_a_name_the_lookup_misses_uses_the_address_in_the_goal(backend):
    """What the real reader does: recipient_names=["fayaz"], address dropped.
    Before this, RELAY asked for an address that was on screen already."""
    plan = _plan_with(backend, _emailing("fayaz"), f"Email fayaz {FAYAZ} the Q3 report.")

    assert not plan.open_questions
    assert _draft(plan)["to"] == [FAYAZ]
    assert [c["email"] for c in backend.find_contact("fayaz")] == [FAYAZ]


def test_a_missing_contact_with_no_address_still_asks(backend):
    """The question must survive: not finding an address is not permission
    to invent one."""
    plan = _plan_with(backend, _emailing("fayaz"), "Email fayaz the Q3 report.")

    assert plan.open_questions
    assert "What is their address?" in plan.open_questions[0]
