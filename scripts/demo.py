"""The example goal, end to end in a terminal, against the mock backend.

This is build step 5's exit check: "the example goal runs end to end in a
terminal". It approves everything automatically - the point is to show the loop
pausing at each gate, not to replace the UI.

Run it:  .venv/Scripts/python.exe scripts/demo.py
         .venv/Scripts/python.exe scripts/demo.py --reject
         .venv/Scripts/python.exe scripts/demo.py "your own goal, in plain language"
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from relay.config import load_env  # noqa: E402

load_env()

from relay.audit import AuditLog  # noqa: E402
from relay.memory import Memory  # noqa: E402
from relay.loop import Awaiting, new_loop  # noqa: E402
from relay.planner import (  # noqa: E402
    Choices,
    GeminiIntentReader,
    IntentReader,
    Planner,
    PlanningError,
    RegexIntentReader,
)
from relay.schemas import Decision  # noqa: E402
from relay.tools import MockBackend  # noqa: E402

DEFAULT_GOAL = (
    "Email Rahul Menon the Q3 report and set a 30-minute review with him and Anu next week."
)

RULE = "-" * 72


def pick_reader() -> tuple[IntentReader, str]:
    """Gemini when a key is configured, the regex otherwise - and say which.

    This matters for a demo: the regex only handles the example goal and close
    variants, so a run that silently used it would make RELAY look far more
    brittle than it is. Printing the reader keeps that honest.
    """
    if os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_MODEL"):
        reader = GeminiIntentReader()
        extra = (
            f", falling back to {', '.join(reader.models[1:])}"
            if len(reader.models) > 1
            else " (no fallback configured)"
        )
        return reader, f"Gemini {reader.model}{extra}"
    return RegexIntentReader(), "offline regex - handles the example goal and close variants"


def main(goal: str = DEFAULT_GOAL, reject: bool = False) -> int:
    # A throwaway database, so the demo never pollutes the repo.
    db = pathlib.Path(tempfile.mkdtemp()) / "demo.db"
    backend = MockBackend()
    reader, description = pick_reader()
    planner = Planner(
        backend=backend,
        reader=reader,
        today=dt.date(2026, 10, 27),  # fixed, so the dates match the build plan
        # The demo uses a throwaway database, so it remembers only this run -
        # enough to show the mechanism, not enough to answer about last week.
        memory=Memory(db),
    )
    loop = new_loop(planner, AuditLog(db), run_id="demo")

    print(f"{RULE}\nGOAL   : {goal}\nREADER : {description}\n{RULE}")
    try:
        status = loop.start(goal)
    except PlanningError as exc:
        # A goal RELAY cannot do is a result, not a crash.
        print(f"\nREFUSED: {exc}")
        return 0

    while not status.finished:
        if status.awaiting is Awaiting.ANSWER:
            print("\nRELAY needs an answer:")
            for question in status.questions:
                print(f"  ? {question}")
            print("\n  -> answering: the FINAL pdf")
            status = loop.answer(Choices(files={"Q3 report": "f_q3_final"}))
            continue

        card = status.card
        assert card is not None
        print(f"\nAPPROVAL NEEDED - step {card.step_number}: {card.intent}")
        if card.detail:
            for line in card.detail.splitlines():
                print(f"  | {line}")

        if reject:
            print("\n  -> rejecting")
            status = loop.decide(card.step_number, Decision(verdict="reject", note="not today"))
        else:
            print("\n  -> approving")
            status = loop.decide(card.step_number, Decision(verdict="approve"))

    result = loop.result()
    answered = getattr(reader, "last_model_used", None)
    if answered and answered != getattr(reader, "model", None):
        # A quiet downgrade changes plan quality, so say which model wrote this.
        print(f"\nNOTE: the primary model was unavailable; {answered} planned this run.")
    print(f"\n{RULE}")
    print(f"STOPPED: {result.stop_state.value}")
    print(f"SUMMARY: {result.summary}")
    print(f"steps completed: {result.completed_steps}")
    print(f"tool calls: {result.tool_calls_used} of 20   replans: {result.replans_used} of 2")

    # The evidence, counted the way the eval report counts it.
    rows = list(loop.audit.rows(loop.run_id))
    unapproved = [
        r for r in rows if r["tool"] in {"send_email", "create_event"} and not r["token"]
    ]
    print(f"unapproved side effects: {len(unapproved)}")
    print(f"emails sent: {backend.sent_count}   events booked: {backend.event_count}")
    print(RULE)
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(main(goal=args[0] if args else DEFAULT_GOAL, reject="--reject" in sys.argv))
