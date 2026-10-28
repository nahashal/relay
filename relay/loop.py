"""The agent loop: understand, plan, approve, act, verify, stop.

LESSON 13 - the loop is driven, not autonomous.
RELAY waits for a human twice: to answer a question and to approve a side
effect. A loop that ran to completion on its own would have to *hold* those
waits inside itself - a thread parked on input, or a callback - and neither
survives a page reload or a restarted server.

So this loop never blocks. ``advance`` runs as far as it can and then returns
*why* it stopped: a question, a card, or a stop state. The caller (the API, a
test, a terminal demo) supplies the next human input and calls back in. Every
pause is therefore a value, which is also what makes the five stop states
testable.

LESSON 14 - resolve, then hash, then show.
This is the order that matters most in the whole project. A plan's send step
says "send the draft from step 1", and that is not approvable: hashing it would
bind the token to the *placeholder*, so the same token would authorise sending
any draft. ``bind_action`` resolves the reference against real results first;
only then is a card built, shown and hashed. Get this order wrong and the
approval gate still passes all its tests while guaranteeing nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from .approvals import ApprovalError, mint_token
from .audit import AuditLog
from .executor import BudgetExceeded, Executor, UnboundAction
from .planner import Choices, Planner, PlanningError
from .schemas import (
    REF_KEY,
    Action,
    Card,
    Decision,
    Plan,
    Question,
    RunResult,
    StepRef,
    StopState,
    ToolName,
    is_ref,
)


class BindingError(RuntimeError):
    """A reference pointed at something that is not there. Maps to BLOCKED."""


class Awaiting(StrEnum):
    """What the loop is waiting for. ``NOTHING`` means the run is over."""

    NOTHING = "nothing"
    ANSWER = "answer"
    APPROVAL = "approval"


class RunStatus(BaseModel):
    """Everything the caller needs to know after a turn of the loop."""

    run_id: str
    awaiting: Awaiting
    stop_state: StopState | None = None
    questions: list[str] = []
    #: The same questions, structured, so a UI can offer the candidates as
    #: choices instead of parsing the prose above.
    asks: list[Question] = []
    #: Steps RELAY planned but cannot perform. Shown as a checklist.
    checklist: list[str] = []
    card: Card | None = None
    summary: str = ""
    completed_steps: list[int] = []
    tool_calls_used: int = 0
    replans_used: int = 0
    retries_used: int = 0

    @property
    def finished(self) -> bool:
        return self.awaiting is Awaiting.NOTHING


# --------------------------------------------------------------------------- #
# Late binding
# --------------------------------------------------------------------------- #


def resolve_ref(ref: StepRef, results: dict[int, Any]) -> Any:
    """Read the value a reference points at, or raise ``BindingError``.

    Every failure here is a real-world outcome rather than a crash: an empty
    slot list means "no common free time", which the loop reports as BLOCKED
    with options. So the messages are written to be read by a person.
    """
    if ref.from_step not in results:
        raise BindingError(f"step {ref.from_step} has not run yet")

    value = results[ref.from_step]

    if ref.field is not None:
        if not isinstance(value, dict) or ref.field not in value:
            raise BindingError(f"step {ref.from_step} produced no {ref.field!r}")
        return value[ref.field]

    if ref.index is not None:
        if not isinstance(value, list):
            raise BindingError(f"step {ref.from_step} did not produce a list")
        if not value:
            raise BindingError(
                f"step {ref.from_step} found no options - there is nothing to choose from"
            )
        if ref.index >= len(value):
            raise BindingError(
                f"step {ref.from_step} returned {len(value)} options,"
                f" so option {ref.index + 1} does not exist"
            )
        return value[ref.index]

    return value


def bind_action(action: Action, results: dict[int, Any]) -> Action:
    """Return a copy of ``action`` with every reference replaced by a real value.

    Returns a new Action rather than mutating: the unbound original stays in the
    plan, so the audit trail shows both what was planned and what was run.
    """
    if action.is_bound:
        return action

    bound: dict[str, Any] = {}
    for key, value in action.args.items():
        if is_ref(value):
            bound[key] = resolve_ref(StepRef.model_validate(value[REF_KEY]), results)
        elif isinstance(value, list):
            bound[key] = [
                resolve_ref(StepRef.model_validate(item[REF_KEY]), results)
                if is_ref(item)
                else item
                for item in value
            ]
        else:
            bound[key] = value
    return Action(tool=action.tool, args=bound)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


@dataclass
class AgentLoop:
    """One run: a goal, a plan, and the steps between it and a stop state."""

    planner: Planner
    executor: Executor
    audit: AuditLog
    max_replans: int = 2
    #: Automatic retries for a FAILED read-only tool. Side effects are never
    #: retried automatically, whatever this is set to - see LESSON 26.
    max_retries: int = 1

    goal: str = ""
    choices: Choices = field(default_factory=Choices)
    plan: Plan | None = None
    #: step number -> that step's result, for late binding
    results: dict[int, Any] = field(default_factory=dict)
    cursor: int = 0
    replans_used: int = 0
    retries_used: int = 0
    #: How many slots to skip when re-planning. Rejecting a meeting means "not
    #: that time", so the next plan must not offer the same one back.
    slot_offset: int = 0
    completed: list[int] = field(default_factory=list)
    stop_state: StopState | None = None
    summary: str = ""
    #: The bound action currently on the table, and the step it belongs to.
    pending: tuple[int, Action] | None = None
    questions: list[str] = field(default_factory=list)
    asks: list[Question] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.executor.run_id

    # --- entry points -------------------------------------------------------

    def start(self, goal: str) -> RunStatus:
        self.goal = goal
        self.audit.start_run(self.run_id, goal)
        return self._plan_and_advance()

    def answer(self, choices: Choices) -> RunStatus:
        """Supply answers to the open questions and re-plan.

        Answering costs a replan. With the planner collecting *all* its questions
        at once, a goal needs one answering round, so a limit of two leaves room
        for a rejection-driven revision as well.
        """
        if self.stop_state is not None and self.stop_state is not StopState.NEEDS_INPUT:
            raise RuntimeError(f"run already finished in {self.stop_state}")

        self.choices = Choices(
            contacts={**self.choices.contacts, **choices.contacts},
            files={**self.choices.files, **choices.files},
        )
        return self._replan("answered a question")

    def decide(self, step_number: int, decision: Decision) -> RunStatus:
        """Approve, edit or reject the card currently on the table."""
        if self.pending is None:
            raise RuntimeError("nothing is awaiting approval")

        pending_step, pending_action = self.pending
        if step_number != pending_step:
            raise RuntimeError(
                f"step {step_number} is not the step awaiting approval ({pending_step})"
            )

        if decision.verdict == "reject":
            if decision.request_revision:
                if pending_action.tool is ToolName.CREATE_EVENT:
                    # Rejecting a time and being offered it again is not a
                    # revision. This is what the slot radio list used to do,
                    # moved to where rejecting already means "not that one".
                    self.slot_offset += 1
                return self._replan(decision.note or "rejected, revision requested")
            return self._stop(StopState.REJECTED, decision.note or "rejected by the user")

        if decision.verdict == "edit":
            if decision.revised_action is None:
                raise RuntimeError("an edit must carry revised_action")
            revised = bind_action(decision.revised_action, self.results)
            if not revised.is_bound:
                return self._stop(
                    StopState.BLOCKED, "the edited action still references a missing step"
                )
            # An edit authorises nothing: a fresh card goes back for approval,
            # and the token is only minted when that card is approved.
            self.pending = (pending_step, revised)
            return self._status(Awaiting.APPROVAL, card=self._card(pending_step, revised))

        # --- approve ---------------------------------------------------------
        # The token is minted over the BOUND action - the same object the card
        # showed. This is the line Lesson 15 is about.
        token, digest = mint_token(self.run_id, pending_action)
        self.audit.record_approval(
            token, self.run_id, digest, decision.verdict, decision.note
        )
        stopped = self._run_step(pending_step, pending_action, token)
        return stopped if stopped is not None else self.advance()

    # --- internals ----------------------------------------------------------

    def _plan_and_advance(self) -> RunStatus:
        try:
            self.plan = self.planner.plan(self.goal, self.choices, self.slot_offset)
        except PlanningError as exc:
            return self._stop(StopState.BLOCKED, str(exc))

        self.audit.record_plan(self.run_id, self.replans_used, self.plan.model_dump_json())
        self.cursor = 0
        self.results.clear()  # a new plan's step numbers mean new things
        self.completed.clear()

        if self.plan.needs_input:
            self.questions = list(self.plan.open_questions)
            self.asks = list(self.plan.questions)
            self.stop_state = StopState.NEEDS_INPUT
            self.audit.finish_run(self.run_id, StopState.NEEDS_INPUT, "; ".join(self.questions))
            return self._status(Awaiting.ANSWER)

        self.questions = []
        self.asks = []
        self.stop_state = None
        return self.advance()

    def _replan(self, reason: str) -> RunStatus:
        if self.replans_used >= self.max_replans:
            return self._stop(
                StopState.BUDGET_HIT,
                f"replan limit of {self.max_replans} reached ({reason}); stopping instead of looping",
            )
        self.replans_used += 1
        self.pending = None
        return self._plan_and_advance()

    def advance(self) -> RunStatus:
        """Run read-only steps until a human is needed or the run stops."""
        assert self.plan is not None

        while self.cursor < len(self.plan.steps):
            step = self.plan.steps[self.cursor]

            try:
                action = bind_action(step.action, self.results)
            except BindingError as exc:
                # e.g. the slot search came back empty: a hard failure, reported
                # with the reason rather than retried. The step's own wording is
                # included because "step 3 found no options" means nothing to
                # someone who is looking at a calendar, not at a plan.
                return self._stop(
                    StopState.BLOCKED, f"Cannot do step {step.number}, {step.intent.lower()}: {exc}"
                )

            if action.has_side_effect:
                self.pending = (step.number, action)
                return self._status(Awaiting.APPROVAL, card=self._card(step.number, action))

            stopped = self._run_step(step.number, action, token=None)
            if stopped is not None:
                return stopped

        # Falling out of the loop without a finish() step still has to stop.
        return self._stop(StopState.DONE, self.summary or "all steps completed")

    def _run_step(self, step_number: int, action: Action, token: str | None) -> RunStatus | None:
        """Execute one bound action and move the cursor.

        Returns ``None`` to mean "that worked, keep going" - the caller's loop
        decides what comes next. Returning a status means the run paused or
        stopped, and the caller must hand it straight back.
        """
        # LESSON 26 - what may be retried is decided by the side effect, not the
        # error. A failed lookup can be repeated freely: worst case we read the
        # same calendar twice. A failed SEND cannot, because the failure mode
        # that matters most - a timeout - is exactly the one where the message
        # may already have gone. Retrying there risks the one thing the whole
        # project exists to prevent, so a failed side effect stops the run and
        # asks a human to look.
        attempts = 1 if action.has_side_effect else self.max_retries + 1
        outcome = None

        for attempt in range(1, attempts + 1):
            try:
                outcome = self.executor.run_action(action, token=token)
            except BudgetExceeded as exc:
                return self._stop(StopState.BUDGET_HIT, str(exc))
            except (ApprovalError, UnboundAction) as exc:
                # Both mean the gate or the binder refused. Neither is
                # recoverable inside the run: stop rather than retry.
                return self._stop(StopState.BLOCKED, str(exc))

            if outcome.ok:
                break
            if attempt < attempts:
                # Each retry costs a tool call, so the budget still bounds this.
                self.retries_used += 1

        assert outcome is not None
        if not outcome.ok:
            reason = outcome.error or f"{action.tool.value} failed"
            if action.has_side_effect:
                reason = (
                    f"{reason}. This was a send or a booking, so RELAY will not retry it"
                    " automatically - it may already have happened. Check, then approve again"
                    " if it did not."
                )
            return self._stop(StopState.BLOCKED, reason)

        self.pending = None
        self.results[step_number] = outcome.result
        self.completed.append(step_number)
        self.cursor += 1

        if action.tool is ToolName.ASK_USER:
            # The plan itself wants an answer before going further.
            question = str(action.args.get("question", ""))
            self.questions = [question]
            # An ask_user step carries prose only; there are no candidates to
            # offer, so the UI falls back to a free-text answer.
            self.asks = [Question(text=question, kind="other", key=question)]
            self.stop_state = StopState.NEEDS_INPUT
            self.audit.finish_run(self.run_id, StopState.NEEDS_INPUT, "; ".join(self.questions))
            return self._status(Awaiting.ANSWER)

        if action.tool is ToolName.FINISH:
            return self._stop(StopState.DONE, self._report())

        return None  # keep going

    # --- support for narrow edits in the UI ---------------------------------

    def options_for(self, step_number: int) -> list[Any]:
        """The full candidate list behind an indexed reference, or ``[]``.

        The loop proposes ``index=0``; a UI that only showed that one option
        would be hiding the other free slots the search already found. The plan
        still holds the *unbound* action, so the reference is still there to
        read even though the card shows a single resolved slot.
        """
        if self.plan is None:
            return []
        step = next((s for s in self.plan.steps if s.number == step_number), None)
        if step is None:
            return []
        for ref in step.action.refs:
            if ref.index is not None:
                candidates = self.results.get(ref.from_step)
                if isinstance(candidates, list):
                    return candidates
        return []

    def redraft(self, to: list[str], subject: str, body: str, attachments: list[str]) -> str:
        """Build a replacement draft and return its id.

        Editing an email cannot mean editing an approved action in place: the
        draft is what gets sent, so a changed recipient or subject has to become
        a *new* draft. That new draft id changes the action hash, which is
        precisely why the edited card needs approving again.

        Drafting has no side effect, so this needs no token - but it goes through
        the executor so it is still audited and still costs a tool call.
        """
        outcome = self.executor.run_action(
            Action(
                tool=ToolName.DRAFT_EMAIL,
                args={"to": to, "subject": subject, "body": body, "attachments": attachments},
            )
        )
        if not outcome.ok or not isinstance(outcome.result, dict):
            raise BindingError(outcome.error or "could not build the edited draft")
        return str(outcome.result["draft_id"])

    def _stop(self, state: StopState, summary: str) -> RunStatus:
        self.stop_state = state
        self.summary = summary
        self.pending = None
        self.audit.finish_run(self.run_id, state, summary)
        return self._status(Awaiting.NOTHING)

    def checklist(self) -> list[str]:
        """Steps RELAY planned but cannot perform, in order.

        Read off the results, so a note only appears here once the run actually
        reached that step - a plan is not a report.
        """
        return [
            str(result["note"])
            for _, result in sorted(self.results.items())
            if isinstance(result, dict) and "note" in result
        ]

    def _report(self) -> str:
        """What actually happened, read off the results rather than assumed."""
        acted: list[str] = []
        answers: list[str] = []
        for _, result in sorted(self.results.items()):
            if not isinstance(result, dict):
                continue
            if "answer" in result:
                # An answer is the whole point of the run, so it leads the
                # report rather than being appended after "Done:".
                answers.append(str(result["answer"]))
            elif result.get("dry_run"):
                # Approved and rehearsed. Saying "nothing needed doing" here
                # told the user their approval had no effect, which is a worse
                # lie than saying it was sent.
                what = {
                    "send_email": "the email would have been sent",
                    "create_event": "the meeting would have been booked",
                }.get(str(result.get("tool", "")), "an action would have run")
                acted.append(f"{what} (dry run)")
            elif "message_id" in result:
                acted.append("the email was sent")
            elif "event_id" in result:
                acted.append("the meeting was booked")

        parts: list[str] = answers[:]
        if acted:
            parts.append("Done: " + ", ".join(acted) + ".")
        pending = self.checklist()
        if pending:
            # Named "over to you" rather than folded into "done": RELAY did not
            # do these, and a report that blurs the two is how an agent gets
            # credit for work nobody did.
            parts.append(
                "Over to you:\n"
                + "\n".join(f"  {i}. {note}" for i, note in enumerate(pending, 1))
            )
        return "\n\n".join(parts) if parts else "Nothing needed doing."

    def _card(self, step_number: int, action: Action) -> Card:
        assert self.plan is not None
        step = next(s for s in self.plan.steps if s.number == step_number)
        return Card(
            step_number=step_number,
            intent=step.intent,
            action=action,
            needs_approval=action.has_side_effect,
            detail=self._detail(action),
        )

    def _detail(self, action: Action) -> str | None:
        """Plain wording of the resolved action, so the card is readable.

        The user should not have to read a dict to know what they are approving.
        """
        args = action.args
        if action.tool is ToolName.SEND_EMAIL:
            draft = self.executor.backend.get_draft(str(args["draft_id"]))
            if draft is None:
                return None
            attached = (
                f" with {len(draft['attachments'])} attachment(s)" if draft["attachments"] else ""
            )
            return (
                f"Send to {', '.join(draft['to'])}{attached}\n"
                f"Subject: {draft['subject']}\n\n{draft['body']}"
            )
        if action.tool is ToolName.CREATE_EVENT:
            slot = args.get("slot") or {}
            if not isinstance(slot, dict):
                return None
            attendees = list(args.get("attendees", []))  # type: ignore[arg-type]
            lines = [
                f"Book {args.get('title')} from {slot.get('start')} to {slot.get('end')}",
                f"Inviting: {', '.join(attendees)}",
            ]
            # What the recipients will actually receive. Approving a send you
            # cannot read is the thing the gate exists to prevent.
            describe = getattr(self.executor.backend, "describe_event", None)
            if callable(describe):
                try:
                    lines += ["", describe(slot, attendees, str(args.get("title")))]
                except Exception as exc:  # noqa: BLE001
                    # A preview is a courtesy and must not take down the card
                    # it belongs to - but a SILENT failure here is how a
                    # missing preview looked exactly like a working one.
                    lines += ["", f"(could not render the invitation: {exc})"]
            return "\n".join(lines)
        return None

    def _status(self, awaiting: Awaiting, card: Card | None = None) -> RunStatus:
        return RunStatus(
            run_id=self.run_id,
            awaiting=awaiting,
            stop_state=self.stop_state,
            questions=list(self.questions),
            asks=list(self.asks),
            checklist=self.checklist(),
            card=card,
            summary=self.summary,
            completed_steps=list(self.completed),
            tool_calls_used=self.executor.calls_used,
            replans_used=self.replans_used,
            retries_used=self.retries_used,
        )

    def result(self) -> RunResult:
        """The final report. Only meaningful once the run has stopped."""
        if self.stop_state is None:
            raise RuntimeError("run has not stopped yet")
        return RunResult(
            stop_state=self.stop_state,
            summary=self.summary,
            tool_calls_used=self.executor.calls_used,
            replans_used=self.replans_used,
            completed_steps=list(self.completed),
        )


def new_loop(
    planner: Planner,
    audit: AuditLog,
    *,
    run_id: str | None = None,
    max_tool_calls: int = 20,
    max_replans: int = 2,
    max_retries: int = 1,
    dry_run: bool = False,
) -> AgentLoop:
    """Build a loop and its executor over the planner's own backend.

    They must share a backend: the draft created in step 1 has to be the draft
    the send step looks up, and two backends would mean two separate worlds.
    """
    executor = Executor(
        backend=planner.backend,
        audit=audit,
        run_id=run_id or uuid.uuid4().hex,
        max_tool_calls=max_tool_calls,
        dry_run=dry_run,
    )
    return AgentLoop(
        planner=planner,
        executor=executor,
        audit=audit,
        max_replans=max_replans,
        max_retries=max_retries,
    )
