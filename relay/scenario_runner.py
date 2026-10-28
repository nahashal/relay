"""Running a scenario end to end, the same way for tests and for the report.

One runner, two callers: `tests/test_scenarios.py` asserts on what it returns,
and `scripts/eval_report.py` tabulates it. A scenario cannot therefore pass in
the suite and fail in the report, which is the whole reason this is not two bits
of similar-looking code.
"""

from __future__ import annotations

from pathlib import Path

from .audit import AuditLog
from .loop import Awaiting, new_loop
from .planner import Planner, RegexIntentReader
from .scenarios import TODAY, Scenario, ScenarioResult
from .schemas import Action, Decision, StopState, ToolName
from .tools import MockBackend

#: Guard against a scenario that never settles. Nothing here should need more:
#: a run pauses at most twice and has at most five steps.
MAX_TURNS = 12


def run_scenario(scenario: Scenario, db_path: Path | str) -> ScenarioResult:
    """Drive one scenario to a stop state and report what happened."""
    backend = MockBackend(
        fail_on=set(scenario.fail_on), fail_always=set(scenario.fail_always)
    )
    if scenario.extra_file:
        backend._data["files"].append(dict(scenario.extra_file))

    audit = AuditLog(db_path)
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TODAY)
    loop = new_loop(
        planner,
        audit,
        run_id=f"scenario_{scenario.number}",
        max_tool_calls=scenario.max_tool_calls,
        max_replans=scenario.max_replans,
    )

    answers = list(scenario.answers)
    decisions = list(scenario.decisions)

    status = loop.start(scenario.goal)
    for _ in range(MAX_TURNS):
        if status.finished:
            break
        if status.awaiting is Awaiting.ANSWER:
            if not answers:
                break  # the scenario expected to stop here
            status = loop.answer(answers.pop(0))
            continue
        if status.awaiting is Awaiting.APPROVAL:
            verdict = decisions.pop(0) if decisions else "approve"
            status = _decide(loop, status, verdict)
            continue
        break

    stop_state = status.stop_state
    unapproved = _unapproved_side_effects(audit, loop.run_id)

    failures: list[str] = []
    if stop_state is not scenario.expect:
        failures.append(f"expected {scenario.expect.value}, got {stop_state and stop_state.value}")
    if backend.sent_count != scenario.expect_emails:
        failures.append(f"expected {scenario.expect_emails} email(s), {backend.sent_count} sent")
    if backend.event_count != scenario.expect_events:
        failures.append(f"expected {scenario.expect_events} event(s), {backend.event_count} booked")
    if unapproved:
        # Never acceptable, in any scenario, whatever else it was testing.
        failures.append(f"{unapproved} side effect(s) ran without an approval token")

    audit.close()
    return ScenarioResult(
        scenario=scenario,
        passed=not failures,
        stop_state=stop_state,
        emails=backend.sent_count,
        events=backend.event_count,
        unapproved=unapproved,
        tool_calls=status.tool_calls_used,
        replans=status.replans_used,
        retries=status.retries_used,
        summary=status.summary,
        failures=failures,
    )


def _decide(loop, status, verdict: str):
    """Turn a scenario's shorthand into a real Decision."""
    step = status.card.step_number

    if verdict == "approve":
        return loop.decide(step, Decision(verdict="approve"))
    if verdict == "reject":
        return loop.decide(step, Decision(verdict="reject"))
    if verdict == "revise":
        return loop.decide(step, Decision(verdict="reject", request_revision=True))
    if verdict == "edit_slot":
        # Pick a different slot than the proposed one, the way the UI does.
        options = loop.options_for(step)
        if len(options) < 2:
            raise AssertionError("edit_slot needs at least two slots to choose between")
        revised = dict(status.card.action.args)
        revised["slot"] = options[1]
        return loop.decide(
            step,
            Decision(
                verdict="edit",
                revised_action=Action(tool=ToolName.CREATE_EVENT, args=revised),
            ),
        )
    raise AssertionError(f"unknown verdict {verdict!r}")


def _unapproved_side_effects(audit: AuditLog, run_id: str) -> int:
    """Side-effect rows with no token. The number that must always be zero."""
    return sum(
        1
        for row in audit.rows(run_id)
        if row["tool"] in {"send_email", "create_event"} and not row["token"]
    )


def run_all(db_dir: Path | str) -> list[ScenarioResult]:
    """Every scenario, each with its own database."""
    from .scenarios import SCENARIOS

    directory = Path(db_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return [
        run_scenario(scenario, directory / f"scenario_{scenario.number}.db")
        for scenario in SCENARIOS
    ]
