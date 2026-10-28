"""The gate tests. These are the ones the build plan's headline claim rests on.

"Zero side-effect actions happen without an approval tied to that exact action"
is not a sentence in a prompt - it is these assertions.
"""

from __future__ import annotations

import pytest

from conftest import ANU, ME, RAHUL, event_action, send_action
from relay.approvals import ApprovalError, mint_token
from relay.executor import BudgetExceeded, Executor
from relay.schemas import Action, ToolName


def test_send_without_token_is_refused(executor, backend, draft_id):
    with pytest.raises(ApprovalError):
        executor.run_action(send_action(draft_id))
    # The real assertion: not merely an exception, but nothing sent.
    assert backend.sent_count == 0


def test_create_event_without_token_is_refused(executor, backend):
    action = event_action(
        {"start": "2026-11-03T12:30:00+05:30", "end": "2026-11-03T13:00:00+05:30"},
        [ME, RAHUL, ANU],
    )
    with pytest.raises(ApprovalError):
        executor.run_action(action)
    assert backend.event_count == 0


def test_read_only_tools_need_no_approval(executor, backend):
    outcome = executor.run_action(Action(tool=ToolName.FIND_CONTACT, args={"name": "Rahul"}))
    assert outcome.ok
    assert len(outcome.result) == 2  # two Rahuls: ambiguity is the default here
    assert backend.sent_count == 0


def test_approved_send_goes_through_and_is_verified(executor, backend, draft_id):
    action = send_action(draft_id)
    token, _ = mint_token(executor.run_id, action)
    executor.audit.record_approval(token, executor.run_id, _, "approve", None)

    outcome = executor.run_action(action, token=token)

    assert outcome.ok and outcome.verified is True
    assert backend.sent_count == 1


def test_token_is_void_if_the_action_changes(executor, backend, draft_id):
    """Approve one email, then try to send a different one with the same token."""
    approved = send_action(draft_id)
    token, digest = mint_token(executor.run_id, approved)
    executor.audit.record_approval(token, executor.run_id, digest, "approve", None)

    other = backend.draft_email(
        to=["priya.nair@relay-test.example"], subject="Q3 report", body="hi", attachments=[]
    )
    tampered = send_action(other["draft_id"])

    with pytest.raises(ApprovalError):
        executor.run_action(tampered, token=token)
    assert backend.sent_count == 0


def test_token_is_single_use(executor, backend, draft_id):
    action = send_action(draft_id)
    token, digest = mint_token(executor.run_id, action)
    executor.audit.record_approval(token, executor.run_id, digest, "approve", None)

    first = executor.run_action(action, token=token)
    assert first.ok

    # Second attempt with the same action replays from the log rather than
    # sending again - the duplicate-send guard from the plan.
    second = executor.run_action(action, token=token)
    assert second.result == {"replayed": True, "note": "already executed in this run"}
    assert backend.sent_count == 1


def test_a_token_from_another_run_does_not_work(executor, backend, draft_id):
    action = send_action(draft_id)
    foreign_token, _ = mint_token("some_other_run", action)
    with pytest.raises(ApprovalError):
        executor.run_action(action, token=foreign_token)
    assert backend.sent_count == 0


def test_forged_token_is_refused(executor, backend, draft_id):
    with pytest.raises(ApprovalError):
        executor.run_action(send_action(draft_id), token="deadbeef.0123456789abcdef")
    assert backend.sent_count == 0


def test_budget_stops_the_run(backend, audit):
    ex = Executor(backend=backend, audit=audit, run_id="run_budget", max_tool_calls=3)
    audit.start_run(ex.run_id, "budget test")
    for _ in range(3):
        ex.run_action(Action(tool=ToolName.FIND_CONTACT, args={"name": "Anu"}))
    with pytest.raises(BudgetExceeded):
        ex.run_action(Action(tool=ToolName.FIND_CONTACT, args={"name": "Anu"}))


def test_dry_run_sends_nothing_even_when_approved(backend, audit, draft_id):
    ex = Executor(backend=backend, audit=audit, run_id="run_dry", dry_run=True)
    audit.start_run(ex.run_id, "dry run")
    action = send_action(draft_id)
    token, digest = mint_token(ex.run_id, action)
    audit.record_approval(token, ex.run_id, digest, "approve", None)

    outcome = ex.run_action(action, token=token)

    # The result names the rehearsed action, so the run report can say
    # "would have been sent" rather than "nothing needed doing".
    assert outcome.result == {"dry_run": True, "tool": "send_email"}
    assert backend.sent_count == 0


def test_audit_log_has_a_token_for_every_side_effect(executor, backend, draft_id):
    """The query behind the 'zero unapproved actions' claim in the eval report."""
    executor.run_action(Action(tool=ToolName.FIND_FILE, args={"query": "Q3 report"}))
    action = send_action(draft_id)
    token, digest = mint_token(executor.run_id, action)
    executor.audit.record_approval(token, executor.run_id, digest, "approve", None)
    executor.run_action(action, token=token)

    side_effect_rows = [r for r in executor.audit.rows(executor.run_id) if r["tool"] == "send_email"]
    assert side_effect_rows and all(r["token"] for r in side_effect_rows)
