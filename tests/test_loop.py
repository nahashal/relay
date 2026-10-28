"""The agent loop: every run ends in exactly one of five named stop states.

This is the file that covers the build plan's second headline claim. There is a
test per stop state, plus the late-binding order that makes the first claim real.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import ANU, ME, RAHUL
from relay.approvals import action_hash, verify_token
from relay.loop import Awaiting, BindingError, bind_action, new_loop, resolve_ref
from relay.planner import Choices, Planner, RegexIntentReader
from relay.schemas import Action, Decision, StepRef, StopState, ToolName
from relay.tools import MockBackend

TUE = dt.date(2026, 10, 27)
EXAMPLE_GOAL = (
    "Email Rahul Menon the Q3 report and set a 30-minute review with him and Anu next week."
)
ANSWERED = Choices(files={"Q3 report": "f_q3_final"})

APPROVE = Decision(verdict="approve")


@pytest.fixture
def loop(backend: MockBackend, audit):
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    return new_loop(planner, audit, run_id="run_loop")


def _run_to_approval(loop, goal: str = EXAMPLE_GOAL, choices: Choices = ANSWERED):
    """Start a run and return the status sitting on its first approval card."""
    status = loop.start(goal)
    if status.awaiting is Awaiting.ANSWER:
        status = loop.answer(choices)
    return status


# --------------------------------------------------------------------------- #
# Stop state 1: DONE
# --------------------------------------------------------------------------- #


def test_the_example_goal_runs_end_to_end(loop, backend):
    """The build plan's own acceptance test, start to finish."""
    status = _run_to_approval(loop)

    # Two approvals: the email, then the meeting.
    assert status.awaiting is Awaiting.APPROVAL
    assert status.card.action.tool is ToolName.SEND_EMAIL
    status = loop.decide(status.card.step_number, APPROVE)

    assert status.awaiting is Awaiting.APPROVAL
    assert status.card.action.tool is ToolName.CREATE_EVENT
    status = loop.decide(status.card.step_number, APPROVE)

    assert status.finished
    assert status.stop_state is StopState.DONE
    assert backend.sent_count == 1 and backend.event_count == 1
    assert "the email was sent" in status.summary
    assert "the meeting was booked" in status.summary


def test_done_means_verified_not_merely_attempted(loop, backend):
    status = _run_to_approval(loop)
    loop.decide(status.card.step_number, APPROVE)
    # Read the state back the way the executor does.
    sent = [r for r in loop.audit.rows(loop.run_id) if r["tool"] == "send_email"]
    assert sent and sent[0]["outcome"] == "ok"


def test_only_two_approvals_are_ever_requested(loop):
    """Read-only steps run without stopping the user."""
    approvals = 0
    status = _run_to_approval(loop)
    while status.awaiting is Awaiting.APPROVAL:
        approvals += 1
        status = loop.decide(status.card.step_number, APPROVE)
    assert approvals == 2
    assert status.stop_state is StopState.DONE


# --------------------------------------------------------------------------- #
# Stop state 2: REJECTED
# --------------------------------------------------------------------------- #


def test_rejecting_without_revision_stops_the_run(loop, backend):
    status = _run_to_approval(loop)
    status = loop.decide(
        status.card.step_number, Decision(verdict="reject", note="wrong attachment")
    )
    assert status.finished and status.stop_state is StopState.REJECTED
    assert backend.sent_count == 0


def test_rejecting_the_meeting_leaves_the_sent_email_alone(loop, backend):
    """A later rejection must not pretend the earlier send did not happen."""
    status = _run_to_approval(loop)
    status = loop.decide(status.card.step_number, APPROVE)  # email goes
    status = loop.decide(status.card.step_number, Decision(verdict="reject"))
    assert status.stop_state is StopState.REJECTED
    assert backend.sent_count == 1 and backend.event_count == 0


# --------------------------------------------------------------------------- #
# Stop state 3: NEEDS_INPUT
# --------------------------------------------------------------------------- #


def test_an_ambiguous_goal_pauses_for_an_answer(loop):
    status = loop.start(EXAMPLE_GOAL)
    assert status.awaiting is Awaiting.ANSWER
    assert status.stop_state is StopState.NEEDS_INPUT
    assert "Several files match" in status.questions[0]
    assert status.card is None  # nothing to approve while a question is open


def test_answering_resumes_the_same_run(loop, backend):
    loop.start(EXAMPLE_GOAL)
    status = loop.answer(ANSWERED)
    assert status.awaiting is Awaiting.APPROVAL
    assert status.replans_used == 1
    # And the answer actually took effect.
    draft_id = status.card.action.args["draft_id"]
    assert backend.get_draft(str(draft_id))["attachments"] == ["f_q3_final"]


def test_two_rahuls_pauses_then_resolves(loop, backend):
    status = loop.start("Email Rahul the Q3 report next week.")
    assert status.awaiting is Awaiting.ANSWER
    assert any("Which Rahul" in q for q in status.questions)

    status = loop.answer(Choices(contacts={"Rahul": RAHUL}, files={"Q3 report": "f_q3_final"}))
    assert status.awaiting is Awaiting.APPROVAL
    draft = backend.get_draft(str(status.card.action.args["draft_id"]))
    assert draft["to"] == [RAHUL]


# --------------------------------------------------------------------------- #
# Stop state 4: BLOCKED
# --------------------------------------------------------------------------- #


def test_no_common_free_slot_blocks_with_a_reason(backend, audit):
    """Priya is busy all week in the fixture, so there is nothing to book."""
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    loop = new_loop(planner, audit, run_id="run_blocked")

    status = loop.start("Set up a 30-minute review with Priya Nair next week.")
    assert status.finished and status.stop_state is StopState.BLOCKED
    assert "nothing to choose from" in status.summary
    assert backend.event_count == 0


def test_a_goal_with_no_tool_step_runs_as_a_checklist(loop):
    """It plans, records the step for the user, and stops cleanly."""
    status = loop.start("Tidy up my desk.")
    assert status.finished and status.stop_state is StopState.DONE
    assert "Tidy up my desk" in status.summary


def test_a_persistent_tool_failure_blocks(audit):
    """Scenario: an API outage on a read-only step. A retry cannot help."""
    backend = MockBackend(fail_always={"draft_email"})
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    loop = new_loop(planner, audit, run_id="run_outage")

    loop.start(EXAMPLE_GOAL)
    status = loop.answer(ANSWERED)
    assert status.finished and status.stop_state is StopState.BLOCKED
    assert "simulated API outage" in status.summary
    assert backend.sent_count == 0


def test_a_transient_read_only_failure_is_retried(audit):
    """Scenario: a timeout, then a retry that works. The run carries on."""
    backend = MockBackend(fail_on={"draft_email"})
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    loop = new_loop(planner, audit, run_id="run_retry")

    loop.start(EXAMPLE_GOAL)
    status = loop.answer(ANSWERED)

    assert status.awaiting is Awaiting.APPROVAL  # recovered, not blocked
    assert status.retries_used == 1
    assert status.tool_calls_used == 2  # the failure cost a call too


def test_a_failed_send_is_never_retried_automatically(audit):
    """The dangerous case: a send that failed may still have gone out."""
    backend = MockBackend(fail_on={"send_email"})
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    loop = new_loop(planner, audit, run_id="run_sendfail")

    loop.start(EXAMPLE_GOAL)
    status = loop.answer(ANSWERED)
    status = loop.decide(status.card.step_number, APPROVE)

    assert status.finished and status.stop_state is StopState.BLOCKED
    assert status.retries_used == 0
    assert "may already have happened" in status.summary


# --------------------------------------------------------------------------- #
# Stop state 5: BUDGET_HIT
# --------------------------------------------------------------------------- #


def test_the_replan_limit_stops_the_run(loop):
    """Three rejections-with-revision exceed the limit of two replans."""
    status = _run_to_approval(loop)
    # The answer already consumed replan 1.
    assert status.replans_used == 1

    status = loop.decide(
        status.card.step_number, Decision(verdict="reject", request_revision=True)
    )
    assert status.replans_used == 2

    status = loop.decide(
        status.card.step_number, Decision(verdict="reject", request_revision=True)
    )
    assert status.finished and status.stop_state is StopState.BUDGET_HIT
    assert "replan limit of 2" in status.summary


def test_the_tool_call_budget_stops_the_run(backend, audit):
    """The budget is checked BEFORE the side effect, so a tight budget means
    an approved send still does not go out."""
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE)
    loop = new_loop(planner, audit, run_id="run_tight", max_tool_calls=1)

    loop.start(EXAMPLE_GOAL)
    status = loop.answer(ANSWERED)
    # One call spent on draft_email, then the send card comes up.
    assert status.awaiting is Awaiting.APPROVAL
    assert status.tool_calls_used == 1

    status = loop.decide(status.card.step_number, APPROVE)
    assert status.finished and status.stop_state is StopState.BUDGET_HIT
    assert "budget" in status.summary
    assert backend.sent_count == 0  # approved, but the budget still stopped it


def test_every_run_ends_in_exactly_one_named_state(loop):
    """The claim itself: there is no other way out."""
    status = _run_to_approval(loop)
    while not status.finished:
        status = loop.decide(status.card.step_number, APPROVE)
    assert status.stop_state in set(StopState)
    assert loop.result().stop_state is status.stop_state


# --------------------------------------------------------------------------- #
# Late binding: resolve, then hash, then show
# --------------------------------------------------------------------------- #


def test_the_plan_carries_references_not_values(loop):
    _run_to_approval(loop)
    send_step = next(s for s in loop.plan.steps if s.action.tool is ToolName.SEND_EMAIL)
    assert not send_step.action.is_bound
    assert send_step.action.refs[0].field == "draft_id"


def test_the_card_shows_a_bound_action(loop):
    status = _run_to_approval(loop)
    assert status.card.action.is_bound
    assert str(status.card.action.args["draft_id"]).startswith("draft_")


def test_the_token_is_minted_over_the_bound_action(loop):
    """The ordering guarantee: what the user read is what the token authorises."""
    status = _run_to_approval(loop)
    card_action = status.card.action

    loop.decide(card_action and status.card.step_number, APPROVE)

    approvals = [
        r for r in loop.audit._db.execute("SELECT * FROM approvals WHERE run_id = ?", (loop.run_id,))
    ]
    assert approvals[0]["action_hash"] == action_hash(card_action)


def test_an_unbound_action_can_never_be_approved(loop, backend):
    """Even with a valid token for it, the executor refuses an unbound action."""
    from relay.approvals import mint_token
    from relay.executor import UnboundAction

    unbound = Action(
        tool=ToolName.SEND_EMAIL,
        args={"draft_id": StepRef(from_step=1, field="draft_id").as_arg()},
    )
    token, _ = mint_token(loop.run_id, unbound)
    assert verify_token(token, loop.run_id, unbound)  # the token itself is genuine

    with pytest.raises(UnboundAction):
        loop.executor.run_action(unbound, token=token)
    assert backend.sent_count == 0


def test_the_card_detail_spells_out_the_real_email(loop):
    status = _run_to_approval(loop)
    assert RAHUL in status.card.detail
    assert "1 attachment" in status.card.detail


def test_the_event_card_shows_the_proposed_slot_and_attendees(loop):
    status = _run_to_approval(loop)
    status = loop.decide(status.card.step_number, APPROVE)
    assert set(status.card.action.args["attendees"]) == {ME, RAHUL, ANU}
    assert "2026-11-" in status.card.detail


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #


def test_editing_requires_a_fresh_approval(loop, backend):
    """An edit authorises nothing - it produces another card."""
    status = _run_to_approval(loop)
    other = backend.draft_email([ANU], "Q3 report", "hi", [])

    revised = Action(tool=ToolName.SEND_EMAIL, args={"draft_id": other["draft_id"]})
    status = loop.decide(
        status.card.step_number, Decision(verdict="edit", revised_action=revised)
    )

    assert status.awaiting is Awaiting.APPROVAL  # still waiting
    assert backend.sent_count == 0  # nothing sent by the edit itself
    assert status.card.action.args["draft_id"] == other["draft_id"]

    status = loop.decide(status.card.step_number, APPROVE)
    assert backend.sent_count == 1


def test_editing_a_slot_books_the_edited_one(loop, backend):
    status = _run_to_approval(loop)
    status = loop.decide(status.card.step_number, APPROVE)  # send the email

    slots = loop.results[3]
    second = slots[1]
    revised = Action(
        tool=ToolName.CREATE_EVENT,
        args={"slot": second, "attendees": [ME, RAHUL, ANU], "title": "Q3 Report review"},
    )
    status = loop.decide(
        status.card.step_number, Decision(verdict="edit", revised_action=revised)
    )
    status = loop.decide(status.card.step_number, APPROVE)

    assert status.stop_state is StopState.DONE
    assert backend.event_count == 1


# --------------------------------------------------------------------------- #
# Binding helpers
# --------------------------------------------------------------------------- #


def test_resolve_ref_reads_a_field():
    assert resolve_ref(StepRef(from_step=1, field="draft_id"), {1: {"draft_id": "d9"}}) == "d9"


def test_resolve_ref_reads_an_index():
    assert resolve_ref(StepRef(from_step=2, index=1), {2: ["a", "b"]}) == "b"


@pytest.mark.parametrize(
    "ref, results",
    [
        (StepRef(from_step=9, field="x"), {}),                 # step has not run
        (StepRef(from_step=1, field="missing"), {1: {"a": 1}}),  # no such field
        (StepRef(from_step=1, index=0), {1: []}),              # nothing to choose from
        (StepRef(from_step=1, index=5), {1: ["a"]}),           # out of range
        (StepRef(from_step=1, index=0), {1: {"a": 1}}),        # not a list
    ],
)
def test_binding_failures_are_explained_not_crashed(ref, results):
    with pytest.raises(BindingError):
        resolve_ref(ref, results)


def test_bind_action_leaves_bound_actions_alone():
    action = Action(tool=ToolName.SEND_EMAIL, args={"draft_id": "d1"})
    assert bind_action(action, {}) is action
