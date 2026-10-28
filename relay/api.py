"""FastAPI surface for the approval UI: run, answer, decide, status.

LESSON 15 - the API shape enforces the gate too.
Note what is missing: there is no endpoint that executes an action by name. The
client can only answer a question the loop asked or decide a card the loop
offered, and the token behind that card was minted server-side over a bound
action. A hostile client can therefore approve nothing RELAY did not itself
propose, and can approve it only in the form RELAY showed.

The loop holds all the state-machine logic. These handlers are deliberately
thin: translate HTTP to a loop call, translate the returned RunStatus back. Any
decision made here instead of in loop.py would be a decision that the terminal
demo and the tests do not exercise.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import dates
from .audit import AuditLog
from .config import load_env
from .backends import (
    BackendError,
    backend_name,
    is_dry_run,
    is_live,
    make_backend,
)
from .loop import AgentLoop, Awaiting, BindingError, RunStatus, new_loop
from .memory import Memory
from .planner import (
    Choices,
    GeminiIntentReader,
    IntentReader,
    Planner,
    RegexIntentReader,
)
from .schemas import Decision, ToolName
from .tools import MockBackend

# Read .env before anything asks for a key. Reading a config file at
# import is fine; creating one would not be.
load_env()

app = FastAPI(title="RELAY", version="0.1.0")

#: Live runs, by id. SQLite holds the durable record; this holds the loop objects
#: for runs in flight. A single-user demo does not need more, and the audit log
#: means a lost process loses progress, never evidence.
_RUNS: dict[str, AgentLoop] = {}

#: The audit log is created on first use, not at import: importing a module must
#: not touch the filesystem. ``configure`` lets a test point it at a tmp_path.
_AUDIT: AuditLog | None = None


def configure(audit: AuditLog) -> None:
    """Point the app at a specific audit log. Called by tests before any request."""
    global _AUDIT
    _AUDIT = audit


def get_audit() -> AuditLog:
    global _AUDIT
    if _AUDIT is None:
        _AUDIT = AuditLog()
    return _AUDIT


def _make_planner(backend: Any) -> Planner:
    """Gemini when a key and a model are configured, the offline reader otherwise.

    Falling back rather than failing keeps the demo runnable with no key and no
    network, which is also how the test suite plans.
    """
    reader: IntentReader = RegexIntentReader()
    if os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_MODEL"):
        reader = GeminiIntentReader()
    # The memory reads the same database the audit log writes, so a run becomes
    # recallable the moment it finishes - no separate store to keep in step.
    return Planner(backend=backend, reader=reader, memory=Memory(get_audit().path))


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class RunRequest(BaseModel):
    goal: str
    #: Answers known up front, so a caller may skip a round trip.
    choices: Choices = Field(default_factory=Choices)


class AnswerRequest(BaseModel):
    run_id: str
    choices: Choices


class DecisionRequest(BaseModel):
    run_id: str
    step_number: int
    decision: Decision


class RedraftRequest(BaseModel):
    """An edited email. Produces a new draft, never patches an approved one."""

    run_id: str
    to: list[str]
    subject: str = ""
    body: str = ""
    attachments: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


STATIC = pathlib.Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The approval UI: one HTML file, no build step."""
    return FileResponse(STATIC / "index.html")


@app.get("/mode")
def mode() -> dict[str, Any]:
    """Which backend this process is using, so the UI can say so out loud.

    A user approving a send deserves to know whether it will reach a real
    inbox. Printing "mock" in small grey text is not enough; this drives a
    banner.
    """
    return {
        "backend": backend_name(),
        "live": is_live(),
        "dry_run": is_dry_run(),
    }


def _loop_for(run_id: str) -> AgentLoop:
    loop = _RUNS.get(run_id)
    if loop is None:
        raise HTTPException(404, "unknown run")
    return loop


def _view(loop: AgentLoop, status: RunStatus) -> dict[str, Any]:
    """RunStatus plus the bits of context a UI needs to render it."""
    body: dict[str, Any] = status.model_dump(mode="json")
    plan = loop.plan
    body["goal"] = loop.goal
    body["window"] = (
        dates.describe(plan.window_start, plan.window_end)
        if plan and plan.window_start and plan.window_end
        else None
    )
    # The whole plan, so the user sees where an approval sits in the sequence.
    body["plan"] = (
        [{"number": s.number, "intent": s.intent, "tool": s.action.tool} for s in plan.steps]
        if plan
        else []
    )

    # Context for the two narrow edits the UI offers. Both are read-only extras:
    # the card itself remains the only thing that can be approved.
    body["slot_options"] = []
    body["draft"] = None
    if status.card is not None:
        action = status.card.action
        if action.tool is ToolName.CREATE_EVENT:
            # Every slot the search found, not just the proposed one.
            body["slot_options"] = loop.options_for(status.card.step_number)
        elif action.tool is ToolName.SEND_EMAIL:
            # The draft's fields, so the edit form opens filled in.
            draft = loop.executor.backend.get_draft(str(action.args["draft_id"]))
            body["draft"] = dict(draft) if draft else None
    return body


@app.post("/run")
def start_run(request: RunRequest) -> dict[str, Any]:
    """Start a run: plan the goal and advance to the first question or card."""
    run_id = uuid.uuid4().hex
    try:
        backend = make_backend()
    except BackendError as exc:
        # A misconfigured live backend must not quietly become a mock run.
        raise HTTPException(503, str(exc)) from exc
    loop = new_loop(
        _make_planner(backend), get_audit(), run_id=run_id, dry_run=is_dry_run()
    )
    _RUNS[run_id] = loop
    if request.choices.contacts or request.choices.files:
        loop.choices = request.choices
    return _view(loop, loop.start(request.goal))


@app.post("/answer")
def answer(request: AnswerRequest) -> dict[str, Any]:
    """Answer the open question(s) and resume the run."""
    loop = _loop_for(request.run_id)
    if loop.stop_state is not None and loop.stop_state.value != "needs_input":
        raise HTTPException(409, f"run already finished in {loop.stop_state.value}")
    try:
        return _view(loop, loop.answer(request.choices))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/decide")
def decide(request: DecisionRequest) -> dict[str, Any]:
    """Approve, edit or reject the card currently awaiting a decision."""
    loop = _loop_for(request.run_id)
    if loop.pending is None:
        raise HTTPException(409, "nothing is awaiting approval")
    try:
        return _view(loop, loop.decide(request.step_number, request.decision))
    except RuntimeError as exc:
        # e.g. deciding a step that is not the one on the table.
        raise HTTPException(409, str(exc)) from exc


@app.get("/status/{run_id}")
def status(run_id: str) -> dict[str, Any]:
    """Where a run stands, without advancing it."""
    loop = _loop_for(run_id)
    awaiting = (
        Awaiting.APPROVAL
        if loop.pending is not None
        else Awaiting.ANSWER
        if loop.questions
        else Awaiting.NOTHING
    )
    card = loop._card(*loop.pending) if loop.pending else None
    return _view(
        loop,
        RunStatus(
            run_id=run_id,
            awaiting=awaiting,
            stop_state=loop.stop_state,
            questions=list(loop.questions),
            card=card,
            summary=loop.summary,
            completed_steps=list(loop.completed),
            tool_calls_used=loop.executor.calls_used,
            replans_used=loop.replans_used,
        ),
    )


@app.post("/redraft")
def redraft(request: RedraftRequest) -> dict[str, str]:
    """Build a replacement draft for an edited email and return its id.

    Drafting is read-only, so this needs no approval - but the new draft id
    changes the action hash, which is exactly why the edited card has to be
    approved again. The client follows this with a ``/decide`` edit.
    """
    loop = _loop_for(request.run_id)
    if loop.pending is None or loop.pending[1].tool is not ToolName.SEND_EMAIL:
        raise HTTPException(409, "no email is awaiting approval")
    if not request.to:
        raise HTTPException(422, "an email needs at least one recipient")
    try:
        draft_id = loop.redraft(
            to=request.to,
            subject=request.subject,
            body=request.body,
            attachments=request.attachments,
        )
    except BindingError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"draft_id": draft_id}


@app.get("/audit/{run_id}")
def audit_trail(run_id: str) -> dict[str, Any]:
    """The evidence behind the run: every tool call, and whether it was approved.

    This is what the eval report's "zero unapproved actions" number is counted
    from, so it is an endpoint rather than a SQL snippet in a document.
    """
    rows = list(get_audit().rows(run_id))
    if not rows:
        raise HTTPException(404, "no audit rows for this run")
    calls = [
        {
            "tool": r["tool"],
            "approved": bool(r["token"]),
            "outcome": r["outcome"],
            "started_at": r["started_at"],
        }
        for r in rows
    ]
    unapproved_side_effects = [
        c for c in calls if c["tool"] in {"send_email", "create_event"} and not c["approved"]
    ]
    return {
        "run_id": run_id,
        "tool_calls": calls,
        "unapproved_side_effects": len(unapproved_side_effects),
    }
