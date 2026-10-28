"""The executor: the only code path to a side effect.

LESSON 6 - put the gate in one function, then it is provable.
There is exactly one place in RELAY where send_email and create_event can be
reached: ``Executor.run_action``. Because there is one door, "zero unapproved
actions" is a property we can test rather than a promise we make. If a future
tool bypasses this module, that is the bug - not a missing prompt line.

Order of checks matters and is not arbitrary:
  1. budget   - refuse before doing work, so a runaway loop costs nothing
  2. gate     - a side effect without a matching token never reaches the backend
  3. replay   - already done in this run? report the first result, do not repeat
  4. spend    - burn the token, so one approval authorises exactly one action
  5. execute  - and only now does anything leave the process
  6. verify   - confirm it actually happened, do not assume the API told the truth
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .approvals import ApprovalError, action_hash, verify_token
from .audit import AuditLog, DuplicateSideEffect
from .schemas import Action, StopState, ToolName
from .tools.base import Backend, ToolError


class BudgetExceeded(RuntimeError):
    """Raised when the run has spent its tool-call budget. Maps to BUDGET_HIT."""


class UnboundAction(RuntimeError):
    """An action still carrying a StepRef reached the executor. A loop bug."""


@dataclass
class ToolOutcome:
    """What one tool call produced, plus whether verification passed."""

    tool: ToolName
    ok: bool
    result: Any = None
    error: str | None = None
    verified: bool | None = None  # None for read-only tools: nothing to verify


@dataclass
class Executor:
    """Runs actions against a backend, under the approval gate and the budget."""

    backend: Backend
    audit: AuditLog
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    max_tool_calls: int = 20
    #: Set true to prove a plan end to end without touching the backend at all.
    dry_run: bool = False

    # --- budget -------------------------------------------------------------

    @property
    def calls_used(self) -> int:
        return self.audit.tool_calls_used(self.run_id)

    def _check_budget(self) -> None:
        if self.calls_used >= self.max_tool_calls:
            raise BudgetExceeded(
                f"tool-call budget of {self.max_tool_calls} reached; stopping instead of looping"
            )

    # --- the one door -------------------------------------------------------

    def run_action(self, action: Action, token: str | None = None) -> ToolOutcome:
        """Execute one action. Side effects require a token minted for this exact action."""
        self._check_budget()

        # An unbound action is not a thing that can be done, so it is also not a
        # thing that can be approved. Refusing here closes the hole the typed
        # reference was introduced to prevent: a token minted over a placeholder
        # would authorise "send <from step 1>", which matches any draft at all.
        if not action.is_bound:
            raise UnboundAction(
                f"{action.tool.value} refused: arguments still reference earlier steps"
                f" ({', '.join(r.describe() for r in action.refs)}). Bind them first."
            )

        digest = action_hash(action)

        if action.has_side_effect:
            # No token, a forged token, or a token minted for a different
            # version of this action: all the same answer.
            if token is None or not verify_token(token, self.run_id, action):
                raise ApprovalError(
                    f"{action.tool.value} refused: no approval matching this exact action."
                    " An approval is void if the recipient, attachment or slot changed."
                )
            # Replay guard. A timeout that is retried must not send twice, so we
            # consult the log rather than trusting the caller to remember.
            if self.audit.already_executed(self.run_id, digest):
                return ToolOutcome(
                    tool=action.tool,
                    ok=True,
                    result={"replayed": True, "note": "already executed in this run"},
                    verified=True,
                )
            # Single-use: spending before executing means a crash mid-send can
            # never leave a live token lying around.
            if not self.audit.spend_approval(token):
                raise ApprovalError(f"{action.tool.value} refused: approval already used.")

        try:
            call_id = self.audit.begin_tool_call(
                self.run_id, action.tool.value, action.args, digest, token
            )
        except DuplicateSideEffect as exc:
            # The database backstop fired, which means the replay guard above was
            # bypassed - a bug in our own logic. Nothing has been sent, so fail
            # the step loudly (the caller maps this to BLOCKED) instead of
            # surfacing a raw DB error as a 500.
            return ToolOutcome(tool=action.tool, ok=False, error=str(exc))

        if self.dry_run and action.has_side_effect:
            outcome = ToolOutcome(
                tool=action.tool,
                ok=True,
                # Name the action: the report has to distinguish "approved and
                # rehearsed" from "nothing happened", and those look identical
                # from a bare {"dry_run": True}.
                result={"dry_run": True, "tool": action.tool.value},
                verified=None,
            )
            self.audit.end_tool_call(call_id, "ok", outcome.result)
            return outcome

        try:
            result = self._dispatch(action)
        except ToolError as exc:
            # A tool failure is data about the world, not a crash: the caller
            # turns it into BLOCKED or one retry.
            self.audit.end_tool_call(call_id, "error", str(exc))
            return ToolOutcome(tool=action.tool, ok=False, error=str(exc))

        verified = self._verify(action, result)
        try:
            self.audit.end_tool_call(call_id, "ok" if verified is not False else "error", result)
        except DuplicateSideEffect as exc:
            # Unreachable while begin_tool_call holds the line, but if it ever
            # fires the action HAS already happened - so report the result rather
            # than losing it, and flag that the log is now inconsistent.
            return ToolOutcome(
                tool=action.tool,
                ok=False,
                result=result,
                error=f"action completed but the audit log rejected it: {exc}",
                verified=verified,
            )
        return ToolOutcome(tool=action.tool, ok=verified is not False, result=result, verified=verified)

    # --- dispatch -----------------------------------------------------------

    def _dispatch(self, action: Action) -> Any:
        """Map an Action onto a backend call. Kept boring on purpose."""
        a = action.args
        match action.tool:
            case ToolName.FIND_CONTACT:
                return self.backend.find_contact(str(a["name"]))
            case ToolName.FIND_FILE:
                return self.backend.find_file(str(a["query"]))
            case ToolName.FIND_FREE_SLOTS:
                return self.backend.find_free_slots(
                    list(a["people"]),  # type: ignore[arg-type]
                    int(a["minutes"]),  # type: ignore[arg-type]
                    a["window_start"],  # type: ignore[index]
                    a["window_end"],  # type: ignore[index]
                )
            case ToolName.DRAFT_EMAIL:
                return self.backend.draft_email(
                    list(a["to"]),  # type: ignore[arg-type]
                    str(a.get("subject", "")),
                    str(a.get("body", "")),
                    list(a.get("attachments", [])),  # type: ignore[arg-type]
                )
            case ToolName.ASK_USER:
                # ask_user has no backend: it is a signal to the loop to pause.
                return {"question": str(a["question"]), "awaiting_answer": True}
            case ToolName.NOTE:
                # Nothing to execute: recording it IS the step. No backend, no
                # side effect, so no approval - the user does this one.
                return {"note": str(a["text"]), "for_the_user": True}
            case ToolName.SEND_EMAIL:
                return self.backend.send_email(str(a["draft_id"]))
            case ToolName.CREATE_EVENT:
                return self.backend.create_event(
                    a["slot"],  # type: ignore[arg-type]
                    list(a["attendees"]),  # type: ignore[arg-type]
                    str(a["title"]),
                )
            case ToolName.FINISH:
                return {"summary": str(a.get("summary", "")), "stop_state": StopState.DONE}
        raise ToolError(f"unknown tool {action.tool!r}")

    # --- verification -------------------------------------------------------

    def _verify(self, action: Action, result: Any) -> bool | None:
        """Did the side effect really land? None means there was nothing to check.

        The plan's wording is "email in Sent, event on the calendar" - we read
        the state back rather than trusting a 200 response.
        """
        if not isinstance(result, dict):
            return None
        match action.tool:
            case ToolName.SEND_EMAIL:
                return self.backend.message_in_sent(str(result["message_id"]))
            case ToolName.CREATE_EVENT:
                return self.backend.event_on_calendar(str(result["event_id"]))
        return None
