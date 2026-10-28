"""A backend that behaves like Google but sends nothing.

LESSON 5 - build the mock first, not last.
Every scenario in the test matrix (two Rahuls, no free slot, an API failure
mid-run) is trivial to stage here and painful to stage against a live account.
Writing the mock first also forces the Backend protocol to be honest: anything
the mock cannot express is something the agent loop should not assume.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from pathlib import Path
from typing import Any

from ..slots import free_slots
from .base import (
    Backend,
    Contact,
    Draft,
    EventReceipt,
    FileMatch,
    SendReceipt,
    Slot,
    ToolError,
)

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "sample_data.json"

def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


class MockBackend(Backend):
    """In-memory Google stand-in. Nothing leaves the process.

    ``fail_on`` stages the "API failure mid-run" scenario: name a method and the
    next call to it raises once, so a retry can be tested without unplugging
    anything.
    """

    def __init__(
        self,
        fixtures: Path | str = FIXTURES,
        fail_on: set[str] | None = None,
        fail_always: set[str] | None = None,
    ) -> None:
        self._data: dict[str, Any] = json.loads(Path(fixtures).read_text("utf-8"))
        self._drafts: dict[str, Draft] = {}
        self._sent: dict[str, SendReceipt] = {}
        self._events: dict[str, EventReceipt] = {}
        self._fail_on = set(fail_on or ())
        #: Methods that fail every time, for the outage case. fail_on is the
        #: transient blip that a retry recovers from; this one never does.
        self._fail_always = set(fail_always or ())
        self._ids = itertools.count(1)

    # --- helpers ------------------------------------------------------------

    def _maybe_fail(self, method: str) -> None:
        """Raise if this method was staged to fail.

        ``fail_on`` fires once and clears - the transient blip a retry survives.
        ``fail_always`` never clears - the outage a retry cannot help with.
        """
        if method in self._fail_always:
            raise ToolError(f"{method}: simulated API outage")
        if method in self._fail_on:
            self._fail_on.discard(method)
            raise ToolError(f"{method}: simulated transient API failure")

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids):04d}"

    @property
    def me(self) -> str:
        return str(self._data["me"])

    # --- read-only tools ----------------------------------------------------

    def find_contact(self, name: str) -> list[Contact]:
        """Substring match, case-insensitive.

        Returning two rows is a feature, not a bug: the planner is supposed to
        ask which Rahul rather than pick the first one.
        """
        self._maybe_fail("find_contact")
        needle = name.strip().lower()
        return [c for c in self._data["contacts"] if needle in c["name"].lower()]

    def remember_contact(self, name: str, email: str) -> bool:
        """In memory only - the mock has no file to write."""
        name, email = name.strip(), email.strip()
        if not name or not email:
            return False
        if any(c["email"].lower() == email.lower() for c in self._data["contacts"]):
            return False
        self._data["contacts"].append(
            {"name": name, "email": email, "note": "learned from a goal you typed"}
        )
        return True

    def describe_event(self, slot: Slot, attendees: list[str], title: str) -> str:
        """The mock sends nothing, and says so rather than implying a send."""
        return (
            f"{title}\n{slot['start']} to {slot['end']}\n\n"
            f"Would invite: {', '.join(attendees)}\n"
            "Mock backend: no invitation is actually sent."
        )

    def find_file(self, query: str) -> list[FileMatch]:
        """All query words must appear in the filename; newest first.

        Newest-first is a presentation choice only - the planner still asks when
        more than one matches, because "most recent" is not the same as "right".
        """
        self._maybe_fail("find_file")
        words = [w for w in query.strip().lower().split() if w]
        matches = [f for f in self._data["files"] if all(w in f["name"].lower() for w in words)]
        return sorted(matches, key=lambda f: f["modified"], reverse=True)

    def find_free_slots(
        self,
        people: list[str],
        minutes: int,
        window_start: dt.date,
        window_end: dt.date,
    ) -> list[Slot]:
        """Gather everyone's busy intervals, then hand them to the shared finder.

        The arithmetic lives in relay/slots.py so this backend and the live one
        cannot drift apart - see LESSON 20 there.
        """
        self._maybe_fail("find_free_slots")

        busy_by_person = self._data["busy"]
        unknown = [p for p in people if p not in busy_by_person]
        if unknown:
            # An unknown attendee means a free/busy lookup we cannot do. Guessing
            # "probably free" is exactly the kind of silent risk the plan bans.
            raise ToolError(f"find_free_slots: no calendar access for {unknown}")

        busy = [
            (_parse(start), _parse(end))
            for person in people
            for start, end in busy_by_person[person]
        ]
        hours = self._data["working_hours"]
        try:
            return free_slots(
                busy,
                minutes,
                window_start,
                window_end,
                timezone=self._data["timezone_name"],
                day_start=hours["start"],
                day_end=hours["end"],
            )
        except ValueError as exc:
            raise ToolError(f"find_free_slots: {exc}") from exc

    def draft_email(
        self, to: list[str], subject: str, body: str, attachments: list[str]
    ) -> Draft:
        """Builds a draft and keeps it. No side effect: a draft is not a send."""
        self._maybe_fail("draft_email")
        if not to:
            raise ToolError("draft_email: at least one recipient is required")
        known = {f["id"] for f in self._data["files"]}
        missing = [a for a in attachments if a not in known]
        if missing:
            raise ToolError(f"draft_email: unknown attachment ids {missing}")
        draft: Draft = {
            "draft_id": self._next_id("draft"),
            "to": list(to),
            "subject": subject,
            "body": body,
            "attachments": list(attachments),
        }
        self._drafts[draft["draft_id"]] = draft
        return draft

    def get_draft(self, draft_id: str) -> Draft | None:
        return self._drafts.get(draft_id)

    # --- side effects -------------------------------------------------------

    def send_email(self, draft_id: str) -> SendReceipt:
        self._maybe_fail("send_email")
        if draft_id not in self._drafts:
            raise ToolError(f"send_email: no such draft {draft_id}")
        receipt: SendReceipt = {
            "message_id": self._next_id("msg"),
            "thread_id": self._next_id("thread"),
        }
        self._sent[receipt["message_id"]] = receipt
        return receipt

    def create_event(self, slot: Slot, attendees: list[str], title: str) -> EventReceipt:
        self._maybe_fail("create_event")
        if not attendees:
            raise ToolError("create_event: at least one attendee is required")
        event_id = self._next_id("evt")
        receipt: EventReceipt = {
            "event_id": event_id,
            "html_link": f"https://calendar.example/event/{event_id}",
        }
        self._events[event_id] = receipt
        # Booking it also makes everyone busy, so a second slot search agrees
        # with reality instead of offering the same slot again.
        for person in attendees:
            self._data["busy"].setdefault(person, []).append([slot["start"], slot["end"]])
        return receipt

    # --- verification -------------------------------------------------------

    def message_in_sent(self, message_id: str) -> bool:
        return message_id in self._sent

    def event_on_calendar(self, event_id: str) -> bool:
        return event_id in self._events

    # --- test introspection -------------------------------------------------

    @property
    def sent_count(self) -> int:
        """Used by tests to assert nothing was sent without approval."""
        return len(self._sent)

    @property
    def event_count(self) -> int:
        return len(self._events)
