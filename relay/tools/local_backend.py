"""A real backend with no Google Cloud: local files, Brevo, and .ics invitations.

Same nine methods as MockBackend and GoogleBackend, so the planner, the loop,
the approval gate and the UI cannot tell the difference. What changes is where
each answer comes from:

    find_contact      a local contacts.json          (was: People API)
    find_file         a local documents/ folder      (was: Drive API)
    find_free_slots   PROPOSED from working hours    (was: Calendar free/busy)
    draft_email       built in memory                (was: Gmail drafts)
    send_email        Brevo                          (was: Gmail send)
    create_event      an .ics invitation, emailed    (was: Calendar insert)

LESSON 34 - say which claim you just weakened, in the code.
Five of those six are straight substitutions: a file instead of an API, a
different mail provider. One is NOT, and glossing over it would be the dishonest
part of this design.

``find_free_slots`` here does not know when anyone is busy. It cannot: free/busy
needs the Calendar API, and Google's CalDAV explicitly does not offer it. So
this backend PROPOSES times inside working hours; it does not FIND times that
are free. The approval card is therefore the only thing standing between a
proposal and a clash, which makes the human approval more load-bearing here than
it is with the Google backend - not less.

The method is named ``find_free_slots`` because the protocol says so. Everything
else in this file calls them proposals, and ``proposes_only`` is True so a UI can
say it out loud.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from ..brevo import BrevoClient
from ..ics import build_invite, parse_slot
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

DEFAULT_DOCUMENTS = Path("documents")
DEFAULT_CONTACTS = Path("contacts.json")

#: How many proposals to offer. Every working-hour slot in a week is ~90 rows,
#: which is a scroll, not a choice. A few per day is a decision someone can make.
MAX_PROPOSALS = 8
PROPOSALS_PER_DAY = 2

#: Bounds for the file search. Pointed at a real Documents folder, an unbounded
#: walk would scan a decade of files on every keystroke-sized query.
MAX_DEPTH = 4
MAX_SCANNED = 5000
MAX_MATCHES = 25

#: Folders that are never what someone means by "my documents".
_SKIP_FOLDERS = frozenset(
    {
        "node_modules", "__pycache__", ".git", ".venv", "venv", "env",
        "AppData", "Library", "System Volume Information", "$RECYCLE.BIN",
        "site-packages", "dist", "build", ".idea", ".vscode",
    }
)


def _normalise(text: str) -> str:
    """Lowercase, and treat _ - . / as spaces.

    So "tax returns", "Tax_Returns" and "tax-returns" are the same search. A person
    typing a goal does not know which separator the folder happened to use.
    """
    lowered = text.lower()
    for separator in ("_", "-", ".", "/", "\\"):
        lowered = lowered.replace(separator, " ")
    return lowered


def _matches(relative_path: str, words: list[str]) -> bool:
    """True when every query word starts a word in the path.

    Prefix-on-a-token, not plain substring. A raw substring search for "sign"
    matches "de-SIGN", which is how a search for a signature turned up four
    design files - the same mid-word problem that makes "an" match "Joanna".
    A prefix still matches the useful case: "sign" finds "signed" and "signature".
    """
    tokens = _normalise(relative_path).split()
    return all(any(token.startswith(word) for token in tokens) for word in words)


class LocalBackend(Backend):
    """Local files for reading, Brevo for sending, .ics for inviting."""

    #: True because find_free_slots cannot see anyone's calendar. Read by the
    #: UI and the setup checker so the limitation is visible, not buried here.
    proposes_only = True

    def __init__(
        self,
        me: str,
        documents: Path | str | None = None,
        contacts: Path | str | None = None,
        brevo: BrevoClient | None = None,
        timezone: str = "Asia/Kolkata",
        working_hours: tuple[str, str] = ("09:00", "18:00"),
    ) -> None:
        self._me = me
        self._documents = Path(documents or os.environ.get("RELAY_DOCUMENTS") or DEFAULT_DOCUMENTS)
        self._contacts_file = Path(
            contacts or os.environ.get("RELAY_CONTACTS") or DEFAULT_CONTACTS
        )
        self._brevo = brevo or BrevoClient()
        self._timezone = timezone
        self._hours = working_hours
        self._drafts: dict[str, Draft] = {}
        self._sent: dict[str, str] = {}
        self._events: dict[str, str] = {}
        self._ids = itertools.count(1)

    @property
    def me(self) -> str:
        return self._me

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids):04d}"

    @staticmethod
    def _dedupe(addresses: list[str]) -> list[str]:
        """Order-preserving dedupe. The organiser is usually in the attendee
        list already, and sending them two copies is a poor first impression."""
        seen: set[str] = set()
        out: list[str] = []
        for address in addresses:
            key = address.lower()
            if key not in seen:
                seen.add(key)
                out.append(address)
        return out

    # --- read-only: local files ---------------------------------------------

    def find_contact(self, name: str) -> list[Contact]:
        """Substring match over contacts.json.

        Two people called Rahul is still the default case, because the whole
        ask-rather-than-guess rule depends on ambiguity being reachable.
        """
        if not self._contacts_file.exists():
            raise ToolError(
                f"no contacts file at {self._contacts_file}."
                ' Create one: [{"name": "Rahul Menon", "email": "...", "note": "Finance"}]'
            )
        try:
            rows = json.loads(self._contacts_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ToolError(f"{self._contacts_file} is not valid JSON: {exc}") from exc

        needle = name.strip().lower()
        if not needle:
            return list(rows)
        found: list[Contact] = []
        for row in rows:
            if needle in str(row.get("name", "")).lower():
                found.append(
                    {
                        "name": row.get("name", ""),
                        "email": row.get("email", ""),
                        "note": row.get("note") or "no note",
                    }
                )
        return found

    def find_file(self, query: str) -> list[FileMatch]:
        """All query words must appear in the file's path; newest first.

        LESSON 44 - match what the user sees, not what the code stores.
        Three assumptions here were wrong the moment this met a real folder:

        * Only the TOP level was listed, so a file one folder down was
          invisible - a file sat in Documents/Tax_Returns and could not be
          found from Documents.
        * Only the FILENAME was matched, so "nhl docs" missed every file inside
          a folder called Tax_Returns. People name things by putting them in a
          folder; the folder is part of the name as far as they are concerned.
        * Separators were taken literally, so "nhl docs" could never match
          "Tax_Returns" - a space and an underscore mean the same thing to the
          person typing.
        """
        if not self._documents.exists():
            raise ToolError(
                f"no documents folder at {self._documents.resolve()} - create it and put files in it"
            )
        words = [w for w in _normalise(query).split() if w]
        if not words:
            raise ToolError("file search needs something to search for")

        matches: list[FileMatch] = []
        for path in self._walk():
            # The path relative to the root, so a folder name counts as part of
            # what the file is called.
            relative = path.relative_to(self._documents).as_posix()
            if _matches(relative, words):
                stat = path.stat()
                matches.append(
                    {
                        # The relative path IS the id: no remote ids to resolve.
                        "id": relative,
                        "name": relative,
                        "modified": dt.datetime.fromtimestamp(
                            stat.st_mtime, dt.timezone.utc
                        ).isoformat(),
                        "mime_type": mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream",
                    }
                )
                if len(matches) >= MAX_MATCHES:
                    # A folder of thousands is a scroll, not a choice. Stop
                    # early rather than hand the planner an unusable list.
                    break
        return sorted(matches, key=lambda f: f["modified"], reverse=True)

    def _walk(self):
        """Files under the documents root, a few levels deep.

        Bounded on purpose: pointed at a real Documents folder this could
        otherwise walk a decade of files on every single search.
        """
        root = self._documents
        stack = [(root, 0)]
        seen = 0
        while stack:
            folder, depth = stack.pop()
            try:
                entries = sorted(folder.iterdir())
            except (PermissionError, OSError):
                continue  # a folder we cannot read is not an error, just skipped
            for path in entries:
                if path.name.startswith(".") or path.name in _SKIP_FOLDERS:
                    continue
                if path.is_dir():
                    if depth < MAX_DEPTH:
                        stack.append((path, depth + 1))
                elif path.is_file():
                    seen += 1
                    if seen > MAX_SCANNED:
                        return
                    yield path

    # --- proposing a time (NOT finding a free one) --------------------------

    def find_free_slots(
        self, people: list[str], minutes: int, window_start: dt.date, window_end: dt.date
    ) -> list[Slot]:
        """Times to PROPOSE, inside working hours. Nobody's calendar is consulted.

        See LESSON 34. ``busy`` is empty because there is no source for it, and
        the result is thinned to a few options per day so the approval card
        offers a choice rather than a list.
        """
        if minutes <= 0:
            raise ToolError("find_free_slots: minutes must be positive")

        try:
            every = free_slots(
                busy=[],
                minutes=minutes,
                window_start=window_start,
                window_end=window_end,
                timezone=self._timezone,
                day_start=self._hours[0],
                day_end=self._hours[1],
            )
        except ValueError as exc:
            raise ToolError(f"find_free_slots: {exc}") from exc

        # Spread the offers across days rather than filling Monday morning.
        by_day: dict[str, list[Slot]] = {}
        for slot in every:
            by_day.setdefault(slot["start"][:10], []).append(slot)

        proposals: list[Slot] = []
        for day in sorted(by_day):
            # Mid-morning and mid-afternoon beat 09:00 and 17:30 for a review.
            day_slots = by_day[day]
            picks = [day_slots[len(day_slots) // 3], day_slots[2 * len(day_slots) // 3]]
            for pick in picks[:PROPOSALS_PER_DAY]:
                if pick not in proposals:
                    proposals.append(pick)
            if len(proposals) >= MAX_PROPOSALS:
                break
        return proposals[:MAX_PROPOSALS]

    # --- drafting -----------------------------------------------------------

    def draft_email(
        self, to: list[str], subject: str, body: str, attachments: list[str]
    ) -> Draft:
        """Build a draft in memory. Nothing is sent, so no approval is needed."""
        if not to:
            raise ToolError("draft_email: at least one recipient is required")
        for name in attachments:
            if self._resolve_document(name) is None:
                raise ToolError(f"draft_email: no such document {name!r}")

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

    def _resolve_document(self, name: str) -> Path | None:
        """The real path for an attachment id, or None if it is not allowed.

        Ids became relative PATHS when the search started walking subfolders,
        and a path can point outside the folder - "../../.ssh/id_rsa" is a
        perfectly good relative path. Attachment ids normally come from our own
        search, but a user answering "which file did you mean?" supplies one
        directly, so the check belongs here rather than in a caller.
        """
        root = self._documents.resolve()
        try:
            candidate = (root / name).resolve()
            candidate.relative_to(root)
        except (ValueError, OSError):
            return None  # outside the folder, or unreadable
        return candidate if candidate.is_file() else None

    def _read_attachments(self, names: list[str]) -> list[tuple[str, bytes]]:
        files: list[tuple[str, bytes]] = []
        for name in names:
            path = self._resolve_document(name)
            if path is None:
                raise ToolError(f"attachment is not inside the documents folder: {name!r}")
            # Send it under its bare filename: the recipient has no use for
            # "Tax_Returns/receipt.pdf" as an attachment name.
            files.append((path.name, path.read_bytes()))
        return files

    # --- side effects -------------------------------------------------------

    def send_email(self, draft_id: str) -> SendReceipt:
        draft = self._drafts.get(draft_id)
        if draft is None:
            raise ToolError(f"send_email: no such draft {draft_id}")

        message_id = self._brevo.send(
            to=draft["to"],
            subject=draft["subject"],
            text=draft["body"],
            attachments=self._read_attachments(draft["attachments"]),
        )
        self._sent[message_id] = draft_id
        # Brevo has no threads, so thread_id mirrors the message. The protocol
        # asks for one; pretending it means more than this would be a lie.
        return {"message_id": message_id, "thread_id": message_id}

    def create_event(self, slot: Slot, attendees: list[str], title: str) -> EventReceipt:
        """Email an .ics invitation, to the attendees AND to the organiser.

        LESSON 45 - the organiser needs the invitation too.
        Without the Calendar API nothing can write to a calendar, so the only
        way an event reaches one is through a mail client that recognises an
        invitation and offers to add it. The first version excluded the
        organiser from the recipients - correct for a real calendar invite,
        where the organiser's copy is created server-side, and exactly wrong
        here: the person running RELAY was the one person whose calendar never
        got the meeting.

        A meeting with nobody but yourself is also allowed now. "Block an hour
        on Thursday" is a real goal, and it was refused for having no guests.

        This is still a real side effect - an email leaves - so it is gated
        exactly like send_email. What it is NOT is a write to anyone's
        calendar: every recipient, the organiser included, still has to accept.
        """
        if not attendees:
            raise ToolError("create_event: at least one attendee is required")

        start, end = parse_slot(slot)
        guests = [person for person in attendees if person != self._me]
        when = start.strftime("%a %d %b %Y, %H:%M")

        def post(to: list[str], method: str, who: list[str]) -> str:
            invite = build_invite(
                start=start,
                end=end,
                title=title,
                organiser=self._me,
                attendees=who,
                description="Sent by RELAY. Accept to add this to your calendar.",
                method=method,
            )
            return self._brevo.send(
                to=to,
                subject=f"Invitation: {title} - {when}",
                text=(
                    f"{title}\n{when} ({end - start})\n\n"
                    "The attached invitation can be accepted from your calendar."
                ),
                attachments=[("invite.ics", invite.encode("utf-8"))],
            )

        # LESSON 46 - the organiser's copy needs a different rendering.
        # Measured, not assumed. A METHOD:PUBLISH invitation naming the
        # recipient as the attendee arrives and offers "add to calendar". A
        # METHOD:REQUEST one was DELIVERED by Gmail and then never shown - and
        # the organiser's copy of a real meeting is worse still, because they
        # appear as ORGANIZER with no ATTENDEE line of their own: an invitation
        # they are not invited to.
        #
        # So the two audiences get different mail, and both halves have since
        # been measured on separate Gmail accounts: the guest's REQUEST arrives
        # as an invitation, the organiser's PUBLISH arrives and adds to the
        # calendar. REQUEST is not broken in general - only an invitation
        # addressed to its own organiser is. Two sends rather than one, which is
        # a cheap price for the organiser's diary actually being updated.
        ids: list[str] = []
        if guests:
            ids.append(post(guests, "REQUEST", guests))
        ids.append(post([self._me], "PUBLISH", [self._me]))

        for message_id in ids:
            self._events[message_id] = title
        return {
            "event_id": ids[0],
            "html_link": "invitation emailed to "
            + ", ".join(self._dedupe([*guests, self._me])),
        }

    # --- verification -------------------------------------------------------

    def message_in_sent(self, message_id: str) -> bool:
        """Did Brevo accept and record this message? (LESSON 33: weaker evidence.)"""
        if message_id not in self._sent and message_id not in self._events:
            return False
        if self._brevo.message_exists(message_id):
            return True
        # Brevo's event feed is asynchronous: a message that will be delivered
        # can be unknown for a few seconds. A recorded messageId from a 2xx is
        # the weaker fallback, and the README says so plainly.
        return True

    def event_on_calendar(self, event_id: str) -> bool:
        """The invitation was sent. Whether anyone ACCEPTED is not knowable here."""
        return self.message_in_sent(event_id)

    # --- introspection ------------------------------------------------------

    @property
    def sent_count(self) -> int:
        return len(self._sent)

    @property
    def event_count(self) -> int:
        return len(self._events)
