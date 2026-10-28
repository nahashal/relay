"""Audit-log behaviour, including the database backstop under the replay guard."""

from __future__ import annotations

import importlib
import pathlib

import pytest

from conftest import RAHUL, send_action
from relay.approvals import action_hash, mint_token
from relay.audit import DuplicateSideEffect


def test_second_successful_side_effect_is_refused_at_insert(audit):
    """The backstop must refuse BEFORE the tool runs, not after.

    This simulates the replay guard being bypassed: insert a side-effect call,
    mark it successful, then try the same hash again.
    """
    audit.start_run("r1", "goal")
    digest = "a" * 64
    call_id = audit.begin_tool_call("r1", "send_email", {"draft_id": "d1"}, digest, "tok.sig")
    audit.end_tool_call(call_id, "ok", {"message_id": "m1"})

    with pytest.raises(DuplicateSideEffect):
        audit.begin_tool_call("r1", "send_email", {"draft_id": "d1"}, digest, "tok.sig")


def test_a_failed_side_effect_may_be_retried(audit):
    """Scenario: retry after a timeout. A failure must not poison the action."""
    audit.start_run("r2", "goal")
    digest = "b" * 64
    first = audit.begin_tool_call("r2", "send_email", {"draft_id": "d1"}, digest, "tok.sig")
    audit.end_tool_call(first, "error", "timeout")

    # Same action again: allowed, because nothing succeeded yet.
    second = audit.begin_tool_call("r2", "send_email", {"draft_id": "d1"}, digest, "tok.sig")
    audit.end_tool_call(second, "ok", {"message_id": "m2"})
    assert audit.already_executed("r2", digest)


def test_read_only_calls_may_repeat_freely(audit):
    audit.start_run("r3", "goal")
    digest = "c" * 64
    for _ in range(3):
        audit.begin_tool_call("r3", "find_contact", {"name": "Anu"}, digest, None)
    assert audit.tool_calls_used("r3") == 3


def test_executor_reports_backstop_as_a_failed_step_not_a_crash(executor, backend, draft_id):
    """A tripped backstop must become a BLOCKED-able outcome, never a raw DB error."""
    action = send_action(draft_id)
    token, digest = mint_token(executor.run_id, action)
    executor.audit.record_approval(token, executor.run_id, digest, "approve", None)

    # Pre-seed a successful row for this exact action, then bypass the executor's
    # own replay guard so only the database can catch the repeat.
    call_id = executor.audit.begin_tool_call(
        executor.run_id, action.tool.value, action.args, action_hash(action), token
    )
    executor.audit.end_tool_call(call_id, "ok", {"message_id": "seeded"})
    executor.audit.already_executed = lambda *_: False  # type: ignore[method-assign]

    outcome = executor.run_action(action, token=token)

    assert outcome.ok is False
    assert "already succeeded" in (outcome.error or "")
    assert backend.sent_count == 0  # the point: nothing was sent


def test_importing_the_api_creates_no_database(tmp_path, monkeypatch):
    """Importing a module must not touch the filesystem."""
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("relay.api")
    importlib.reload(module)
    assert not (tmp_path / "relay.db").exists()
    assert not list(pathlib.Path(tmp_path).glob("*.db"))
