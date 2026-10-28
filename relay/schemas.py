"""The shapes RELAY thinks in.

LESSON 1 - why schemas come first.
The agent loop is only trustworthy if a "plan" is data we can inspect, hash and
replay. If the model returned prose, we could not compute a stable hash of
"send this exact email", and the approval gate in approvals.py would have
nothing to bind a token to. So: Pydantic models first, prompts later.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StopState(StrEnum):
    """The five named ends from the build plan. A run finishes in exactly one.

    There is deliberately no SUCCESS-ish catch-all and no "running" member:
    a value of this type always means the run is over, so code that holds one
    can never accidentally keep looping.
    """

    DONE = "done"              # every step ran and was verified
    REJECTED = "rejected"      # the user declined and asked for no revision
    NEEDS_INPUT = "needs_input"  # a question is unanswered; we pause, we do not guess
    BLOCKED = "blocked"        # hard failure, e.g. no common free slot
    BUDGET_HIT = "budget_hit"  # too many tool calls or replans


class ToolName(StrEnum):
    """The eight tools. A closed set, so a hallucinated tool name fails validation."""

    FIND_CONTACT = "find_contact"
    FIND_FILE = "find_file"
    FIND_FREE_SLOTS = "find_free_slots"
    DRAFT_EMAIL = "draft_email"
    ASK_USER = "ask_user"
    #: A step RELAY cannot perform, written down for the user to do.
    #: LESSON 40: planning is general, acting is not. A goal is broken into
    #: steps whatever it is about; the ones with a tool behind them run, and
    #: the rest become this - visible in the plan, reported at the end, and
    #: never silently dropped or faked.
    NOTE = "note"
    SEND_EMAIL = "send_email"
    CREATE_EVENT = "create_event"
    FINISH = "finish"


#: Tools that change the world outside RELAY. Everything here must pass the
#: approval gate; everything else may run freely. This single set is the whole
#: definition of "side effect" in the codebase - do not duplicate the list.
SIDE_EFFECT_TOOLS: frozenset[ToolName] = frozenset(
    {ToolName.SEND_EMAIL, ToolName.CREATE_EVENT}
)


#: Marker key for a late-bound argument. An arg whose value is
#: ``{"$ref": {...}}`` is not a value yet - it points at an earlier step's
#: result, and ``loop.bind_action`` substitutes the real value before the action
#: is hashed or shown to anyone.
REF_KEY = "$ref"


class StepRef(BaseModel):
    """A pointer to part of an earlier step's result.

    LESSON 12 - a placeholder must be machine-readable, not prose.
    The first draft of the planner emitted ``{"draft_id": "<from step 1>"}``,
    which reads nicely and is useless: nothing can resolve it, and if it ever
    reached ``mint_token`` the approval would be bound to the *placeholder*
    rather than to the real draft. A typed reference cannot be approved by
    accident, because binding it is the only way to get a sendable action.
    """

    model_config = ConfigDict(frozen=True)

    from_step: int = Field(ge=1)
    #: Key to read from a dict result, e.g. "draft_id".
    field: str | None = None
    #: Index into a list result, e.g. the first free slot.
    index: int | None = None

    def as_arg(self) -> dict[str, object]:
        """How this reference appears inside ``Action.args``."""
        return {REF_KEY: self.model_dump(exclude_none=True)}

    def describe(self) -> str:
        """Wording for a plan preview shown before the run starts."""
        where = f"step {self.from_step}"
        if self.field:
            return f"the {self.field} from {where}"
        if self.index is not None:
            return f"option {self.index + 1} from {where}"
        return f"the result of {where}"


def is_ref(value: object) -> bool:
    """True if this argument is still a reference rather than a value."""
    return isinstance(value, dict) and REF_KEY in value


class Action(BaseModel):
    """One concrete tool call the agent wants to make.

    ``args`` is intentionally a plain dict: each tool validates its own
    arguments in tools/, which keeps this model stable as tools are added.

    An action may be *unbound* - carrying one or more ``StepRef`` arguments - in
    which case it is not executable and must not be approved. ``is_bound`` is
    the check, and the executor refuses anything that fails it.
    """

    model_config = ConfigDict(frozen=True)  # frozen so a hashed action cannot drift

    tool: ToolName
    args: dict[str, object] = Field(default_factory=dict)

    @property
    def has_side_effect(self) -> bool:
        return self.tool in SIDE_EFFECT_TOOLS

    @property
    def is_bound(self) -> bool:
        """True when every argument is a real value, so this can be run.

        Checks one level into lists and dicts, which is as deep as any tool's
        arguments go. A nested structure that could hide a reference deeper than
        that would be a reason to change this, not to trust it.
        """
        for value in self.args.values():
            if is_ref(value):
                return False
            if isinstance(value, list) and any(is_ref(item) for item in value):
                return False
            if isinstance(value, dict) and any(is_ref(item) for item in value.values()):
                return False
        return True

    @property
    def refs(self) -> list[StepRef]:
        """Every reference this action still carries."""
        found: list[StepRef] = []
        for value in self.args.values():
            candidates = (
                value
                if isinstance(value, list)
                else list(value.values())
                if isinstance(value, dict) and REF_KEY not in value
                else [value]
            )
            for item in candidates:
                if is_ref(item):
                    found.append(StepRef.model_validate(item[REF_KEY]))  # type: ignore[index]
        return found


class Step(BaseModel):
    """A numbered step of the plan, as shown on the approval card."""

    model_config = ConfigDict(frozen=True)

    number: int = Field(ge=1)
    intent: str = Field(min_length=1)  # plain English, for the human reading the card
    action: Action


class QuestionOption(BaseModel):
    """One candidate answer, as a label to read and a value to send back."""

    model_config = ConfigDict(frozen=True)

    label: str
    value: str


class Question(BaseModel):
    """Something RELAY needs to know, with the candidates it found.

    LESSON 17 - prose is not an interface.
    Open questions started as plain strings, which read well in a terminal and
    are useless to a browser: "Which Rahul did you mean? I found: Rahul Menon
    (Finance), Rahul Shetty (Design team)." cannot be turned back into a
    ``Choices`` payload without parsing English. The UI needs ``kind``, ``key``
    and ``options``; the prose stays as ``text`` for the log and the terminal.
    Both come from one place, so they cannot drift apart.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    #: Which map in ``Choices`` an answer belongs to.
    kind: Literal["contact", "file", "other"]
    #: The name or search phrase as the user wrote it: the key to answer under.
    key: str
    options: tuple[QuestionOption, ...] = ()

    @property
    def answerable(self) -> bool:
        """True when the user can just pick. False means they must type."""
        return bool(self.options)


class Plan(BaseModel):
    """The validated plan the user approves before anything runs."""

    goal: str = Field(min_length=1)
    steps: list[Step] = Field(min_length=1)
    #: Set when the planner resolved a relative phrase like "next week", so the
    #: approval card can show the dates it actually means.
    window_start: dt.date | None = None
    window_end: dt.date | None = None
    #: Questions the planner could not answer on its own. Non-empty means the
    #: run should head for NEEDS_INPUT rather than ask the user to approve a guess.
    #: ``open_questions`` is the prose (terminal, logs); ``questions`` is the
    #: same thing structured (the UI). Built together, never separately.
    open_questions: list[str] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)

    @property
    def needs_input(self) -> bool:
        return bool(self.open_questions)


class Decision(BaseModel):
    """What the user did with one approval card."""

    verdict: Literal["approve", "edit", "reject"]
    #: Present for "edit": the replacement action, which must be approved afresh.
    revised_action: Action | None = None
    note: str | None = None
    #: For "reject": ask the agent for a different plan rather than stopping.
    #: The build plan's wording - "rejecting one action asks the agent to revise
    #: or stop" - is a choice the user makes, so it is a field, not a guess.
    request_revision: bool = False


class Card(BaseModel):
    """What the user is shown before a side effect happens.

    The action here is always *bound*: every reference resolved to a real value.
    That matters because this is the same object that gets hashed, so what the
    user reads and what the token authorises cannot drift apart.
    """

    step_number: int
    intent: str
    action: Action
    needs_approval: bool
    #: Short human summary of what approving this will do.
    detail: str | None = None


class RunResult(BaseModel):
    """What a finished run reports back. Always carries a stop state."""

    stop_state: StopState
    summary: str
    tool_calls_used: int = 0
    replans_used: int = 0
    completed_steps: list[int] = Field(default_factory=list)
