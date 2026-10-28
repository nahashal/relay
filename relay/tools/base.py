"""One interface for all eight tools.

LESSON 4 - why a Backend protocol instead of calling Google directly.
The build plan promises Outlook or GoHighLevel can be added "without touching
the agent loop". That only holds if the loop never imports googleapiclient.
So: the loop knows ``Backend``; MockBackend and (later) GoogleBackend implement
it. The same seam gives us dry-run mode and fast tests for free.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, TypedDict, runtime_checkable


class Contact(TypedDict):
    name: str
    email: str
    note: str  # e.g. "Design team" - shown when two Rahuls force a question


class FileMatch(TypedDict):
    id: str
    name: str
    modified: str
    mime_type: str


class Slot(TypedDict):
    start: str  # ISO 8601 with offset, so the approval card is unambiguous
    end: str


class Draft(TypedDict):
    draft_id: str
    to: list[str]
    subject: str
    body: str
    attachments: list[str]


class SendReceipt(TypedDict):
    message_id: str
    thread_id: str


class EventReceipt(TypedDict):
    event_id: str
    html_link: str


@runtime_checkable
class Backend(Protocol):
    """Everything RELAY needs from the outside world, and nothing more.

    Read-only methods come first; the two that change the world are last and
    are the only ones the executor gates.
    """

    # --- read-only ----------------------------------------------------------

    def find_contact(self, name: str) -> list[Contact]: ...

    #: Teach the backend a contact the USER supplied. True if it was new.
    #: Read-only in the gated sense - nothing leaves the machine - which is
    #: why it sits with the read-only tools and the executor never sees it.
    def remember_contact(self, name: str, email: str) -> bool: ...

    def find_file(self, query: str) -> list[FileMatch]: ...

    #: What create_event will actually do, in words, for the approval card.
    #: Each backend answers for itself: one emails an invitation, another
    #: writes a calendar entry. A card that described only the arguments
    #: left the user approving a send they could not read.
    def describe_event(self, slot: Slot, attendees: list[str], title: str) -> str: ...

    def find_free_slots(
        self, people: list[str], minutes: int, window_start: dt.date, window_end: dt.date
    ) -> list[Slot]: ...

    def draft_email(
        self, to: list[str], subject: str, body: str, attachments: list[str]
    ) -> Draft: ...

    def get_draft(self, draft_id: str) -> Draft | None: ...

    # --- side effects: approval required ------------------------------------

    def send_email(self, draft_id: str) -> SendReceipt: ...

    def create_event(
        self, slot: Slot, attendees: list[str], title: str
    ) -> EventReceipt: ...

    # --- verification: did it really happen? --------------------------------

    def message_in_sent(self, message_id: str) -> bool: ...

    def event_on_calendar(self, event_id: str) -> bool: ...


class ToolError(RuntimeError):
    """A tool failed in a way the run should treat as BLOCKED, not retry forever."""
