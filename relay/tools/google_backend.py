"""The live backend: Gmail, Calendar, Drive and People behind the same protocol.

LESSON 22 - separate the request from the reasoning, or you cannot test it.
Nothing in this file can be tested against the real APIs without a Google
account, a consent screen and a network. So the parts that can be wrong on
their own - escaping a Drive query, building a MIME message, reading a
free/busy response, deciding whether a send actually landed - are plain
functions at the top, taking dicts and returning dicts. The class below is a
thin wrapper that calls an API and hands the result to one of them.

The split is not a testing trick. It is where the bugs live: a mangled query or
a misread response is a far likelier failure than `files().list()` itself.

LESSON 23 - an error is not an empty result.
The sharpest edge in this file is ``parse_busy``. Google's freebusy response
reports per-calendar errors *inside a 200 response*: ask about someone whose
calendar you cannot see and you get `{"errors": [...], "busy": []}`. Read that
carelessly and an inaccessible calendar looks exactly like a free one - so
RELAY would cheerfully book over someone's day. Empty means free; an error
means stop and say so.
"""

from __future__ import annotations

import base64
import datetime as dt
import mimetypes
from email.message import EmailMessage
from typing import Any, Iterable

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

#: Google Workspace documents have no bytes to download; they are exported.
#: Mapping each to the format a recipient can actually open.
_EXPORT_AS = {
    "application/vnd.google-apps.document": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}

#: Attachments larger than this are refused rather than silently failing at the
#: Gmail size limit (25 MB including base64 overhead, so the real ceiling is
#: lower than it looks).
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Pure helpers: no network, no credentials, fully testable
# --------------------------------------------------------------------------- #


def drive_query(text: str) -> str:
    """A Drive ``q`` clause matching files whose name contains ``text``.

    Drive query strings are single-quoted, so a name containing an apostrophe -
    "Q3 report (Tom's copy)" - would end the string early and produce a syntax
    error, or worse a query that means something else. Backslash first, then
    quote: reversing that order would double-escape the backslashes it just added.
    """
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"name contains '{escaped}' and trashed = false"


def parse_contacts(response: dict[str, Any]) -> list[Contact]:
    """Read People API results into contacts, skipping anyone with no address.

    A directory entry without an email cannot be sent anything, so including it
    would only produce a "which Rahul?" question with an unusable answer in it.
    """
    out: list[Contact] = []
    for row in response.get("results", []) or response.get("connections", []) or []:
        person = row.get("person", row)
        emails = person.get("emailAddresses") or []
        if not emails:
            continue
        names = person.get("names") or []
        organisations = person.get("organizations") or []
        note = ""
        if organisations:
            note = organisations[0].get("department") or organisations[0].get("name") or ""
        out.append(
            {
                "name": (names[0].get("displayName") if names else "") or emails[0]["value"],
                "email": emails[0]["value"],
                # The note is what distinguishes two people with one name, so an
                # empty one is worth saying out loud on the question card.
                "note": note or "no department listed",
            }
        )
    return out


def dedupe_contacts(contacts: Iterable[Contact]) -> list[Contact]:
    """One row per address, preserving order.

    Personal contacts and the Workspace directory overlap, so the same person
    arrives twice - and two identical options on a "which Rahul?" card is a
    question with no right answer.
    """
    seen: set[str] = set()
    out: list[Contact] = []
    for contact in contacts:
        key = contact["email"].lower()
        if key not in seen:
            seen.add(key)
            out.append(contact)
    return out


def parse_files(response: dict[str, Any]) -> list[FileMatch]:
    """Read a Drive file list into matches, newest first."""
    files = [
        {
            "id": f["id"],
            "name": f.get("name", ""),
            "modified": f.get("modifiedTime", ""),
            "mime_type": f.get("mimeType", ""),
        }
        for f in response.get("files", [])
    ]
    return sorted(files, key=lambda f: f["modified"], reverse=True)


def parse_busy(
    response: dict[str, Any], people: list[str]
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Busy intervals from a freebusy response - or ``ToolError``.

    See LESSON 23. Three things have to be true before a calendar's silence can
    be read as "free":
      * the calendar appears in the response at all,
      * it carries no ``errors``,
      * and we asked about it.
    Any of those failing means we do not know, and not knowing is not free.
    """
    calendars = response.get("calendars", {})
    busy: list[tuple[dt.datetime, dt.datetime]] = []

    for person in people:
        entry = calendars.get(person)
        if entry is None:
            raise ToolError(
                f"no free/busy information came back for {person};"
                " refusing to assume they are free"
            )
        errors = entry.get("errors") or []
        if errors:
            reasons = ", ".join(e.get("reason", "unknown") for e in errors)
            raise ToolError(
                f"cannot read {person}'s calendar ({reasons});"
                " refusing to assume they are free"
            )
        for interval in entry.get("busy", []):
            busy.append(
                (
                    dt.datetime.fromisoformat(interval["start"].replace("Z", "+00:00")),
                    dt.datetime.fromisoformat(interval["end"].replace("Z", "+00:00")),
                )
            )
    return busy


def build_message(
    to: list[str],
    subject: str,
    body: str,
    attachments: list[tuple[str, str, bytes]],
    sender: str | None = None,
) -> dict[str, str]:
    """A Gmail ``raw`` payload for a message, with attachments.

    Gmail wants URL-safe base64 of the whole RFC 5322 message. ``urlsafe_b64``
    rather than plain base64 is not optional: a '+' or '/' in standard base64
    breaks the JSON transport Gmail uses.
    """
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    if sender:
        message["From"] = sender
    message.set_content(body)

    for filename, mime_type, payload in attachments:
        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise ToolError(
                f"{filename} is {len(payload) // 1024 // 1024} MB;"
                f" the limit is {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB"
            )
        main, _, sub = (mime_type or "application/octet-stream").partition("/")
        message.add_attachment(
            payload, maintype=main or "application", subtype=sub or "octet-stream",
            filename=filename,
        )

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return {"raw": raw}


def export_format(mime_type: str, name: str) -> tuple[str, str] | None:
    """How to export a Google-native file, or ``None`` if it downloads directly.

    Returns ``(export_mime_type, filename)``.
    """
    mapping = _EXPORT_AS.get(mime_type)
    if mapping is None:
        return None
    export_mime, extension = mapping
    filename = name if name.lower().endswith(extension) else f"{name}{extension}"
    return export_mime, filename


def attachment_filename(name: str, mime_type: str) -> str:
    """A filename a recipient can open: add an extension when Drive omits one."""
    if "." in name.rsplit("/", 1)[-1]:
        return name
    guessed = mimetypes.guess_extension(mime_type or "") or ""
    return f"{name}{guessed}"


def was_sent(message: dict[str, Any]) -> bool:
    """True only if Gmail says the message carries the SENT label.

    A 200 from ``drafts.send`` means Gmail accepted the request, not that the
    message exists in Sent. Verification reads the label back - the same reason
    the mock has ``message_in_sent`` at all.
    """
    return "SENT" in (message.get("labelIds") or [])


def event_is_live(event: dict[str, Any]) -> bool:
    """True if the event exists and has not been cancelled.

    A cancelled event still answers ``events.get`` with a 200 and a body, so
    checking only for "no exception" would call a deleted meeting a success.
    """
    return bool(event.get("id")) and event.get("status") != "cancelled"


def rfc3339(moment: dt.datetime) -> str:
    """Google wants RFC 3339; ``isoformat`` gives it, given an aware datetime."""
    if moment.tzinfo is None:
        raise ValueError("refusing to send a naive datetime to Google")
    return moment.isoformat()


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #


class GoogleBackend(Backend):
    """The live implementation. Same eight methods as MockBackend, real effects.

    ``services`` is injected rather than built here, which is what lets the
    tests below drive this class with doubles and no credentials.
    """

    def __init__(
        self,
        services: dict[str, Any],
        me: str,
        timezone: str = "Asia/Kolkata",
        working_hours: tuple[str, str] = ("09:00", "18:00"),
        calendar_id: str = "primary",
    ) -> None:
        self._services = services
        self._me = me
        self._timezone = timezone
        self._hours = working_hours
        self._calendar_id = calendar_id
        #: Drafts this run created, so get_draft answers without a round trip
        #: and the approval card can show what is about to go out.
        self._drafts: dict[str, Draft] = {}

    @property
    def me(self) -> str:
        return self._me

    # --- error handling -----------------------------------------------------

    @staticmethod
    def _call(description: str, request: Any) -> Any:
        """Execute one API request, turning transport errors into ToolError.

        Every failure here is something the run should report and stop on, not
        something to retry blindly: the loop decides, not the tool.
        """
        try:
            from googleapiclient.errors import HttpError
        except ImportError:  # pragma: no cover - only when the SDK is absent
            HttpError = Exception  # type: ignore[assignment,misc]
        try:
            return request.execute()
        except HttpError as exc:  # type: ignore[misc]
            status = getattr(getattr(exc, "resp", None), "status", "?")
            raise ToolError(f"{description} failed ({status}): {exc}") from exc
        except Exception as exc:  # network errors, timeouts
            raise ToolError(f"{description} failed: {exc}") from exc

    # --- read-only tools ----------------------------------------------------

    def remember_contact(self, name: str, email: str) -> bool:
        """Not supported: writing a contact needs a People API write scope,
        and RELAY asks for the narrowest scopes that work. False, not an
        exception - a backend that cannot learn is not a broken run."""
        return False

    def find_contact(self, name: str) -> list[Contact]:
        """Personal contacts plus the Workspace directory, deduped by address."""
        people = self._services["people"]
        read_mask = "names,emailAddresses,organizations"

        found: list[Contact] = []
        found += parse_contacts(
            self._call(
                "contact search",
                people.people().searchContacts(query=name, readMask=read_mask),
            )
        )
        try:
            found += parse_contacts(
                self._call(
                    "directory search",
                    people.people().searchDirectoryPeople(
                        query=name,
                        readMask=read_mask,
                        sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
                    ),
                )
            )
        except ToolError:
            # A personal Gmail account has no directory. That is not a failure
            # of the lookup, so personal contacts still stand on their own.
            pass
        return dedupe_contacts(found)

    def find_file(self, query: str) -> list[FileMatch]:
        self._maybe_warn_empty(query)
        response = self._call(
            "file search",
            self._services["drive"]
            .files()
            .list(
                q=drive_query(query),
                fields="files(id,name,modifiedTime,mimeType)",
                orderBy="modifiedTime desc",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ),
        )
        return parse_files(response)

    @staticmethod
    def _maybe_warn_empty(query: str) -> None:
        if not query.strip():
            # An empty Drive query matches everything; the planner uses that
            # against the mock to list files, but here it would be a 10-file
            # arbitrary sample presented as "the matches".
            raise ToolError("file search needs something to search for")

    def find_free_slots(
        self, people: list[str], minutes: int, window_start: dt.date, window_end: dt.date
    ) -> list[Slot]:
        """Ask Google who is busy, then use the shared finder (LESSON 20)."""
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(self._timezone)
        time_min = dt.datetime.combine(window_start, dt.time(0, 0), tzinfo=zone)
        # The day AFTER the window ends, so the final day is fully included.
        time_max = dt.datetime.combine(
            window_end + dt.timedelta(days=1), dt.time(0, 0), tzinfo=zone
        )

        response = self._call(
            "free/busy lookup",
            self._services["calendar"]
            .freebusy()
            .query(
                body={
                    "timeMin": rfc3339(time_min),
                    "timeMax": rfc3339(time_max),
                    "timeZone": self._timezone,
                    "items": [{"id": person} for person in people],
                }
            ),
        )
        busy = parse_busy(response, people)
        try:
            return free_slots(
                busy,
                minutes,
                window_start,
                window_end,
                timezone=self._timezone,
                day_start=self._hours[0],
                day_end=self._hours[1],
            )
        except ValueError as exc:
            raise ToolError(f"find_free_slots: {exc}") from exc

    def draft_email(
        self, to: list[str], subject: str, body: str, attachments: list[str]
    ) -> Draft:
        """Create a real Gmail draft. A draft is not a send: no approval needed."""
        if not to:
            raise ToolError("draft_email: at least one recipient is required")

        payload = build_message(
            to, subject, body, [self._download(file_id) for file_id in attachments], self._me
        )
        created = self._call(
            "draft creation",
            self._services["gmail"]
            .users()
            .drafts()
            .create(userId="me", body={"message": payload}),
        )
        draft: Draft = {
            "draft_id": created["id"],
            "to": list(to),
            "subject": subject,
            "body": body,
            "attachments": list(attachments),
        }
        self._drafts[draft["draft_id"]] = draft
        return draft

    def get_draft(self, draft_id: str) -> Draft | None:
        return self._drafts.get(draft_id)

    def _download(self, file_id: str) -> tuple[str, str, bytes]:
        """Fetch a Drive file as ``(filename, mime_type, bytes)``."""
        drive = self._services["drive"]
        meta = self._call(
            "file lookup",
            drive.files().get(fileId=file_id, fields="name,mimeType", supportsAllDrives=True),
        )
        name = meta.get("name", file_id)
        mime_type = meta.get("mimeType", "")

        exporting = export_format(mime_type, name)
        if exporting:
            export_mime, filename = exporting
            data = self._call(
                "file export",
                drive.files().export(fileId=file_id, mimeType=export_mime),
            )
            return filename, export_mime, _as_bytes(data)

        data = self._call("file download", drive.files().get_media(fileId=file_id))
        return attachment_filename(name, mime_type), mime_type, _as_bytes(data)

    # --- side effects -------------------------------------------------------

    def send_email(self, draft_id: str) -> SendReceipt:
        sent = self._call(
            "send",
            self._services["gmail"]
            .users()
            .drafts()
            .send(userId="me", body={"id": draft_id}),
        )
        return {"message_id": sent["id"], "thread_id": sent.get("threadId", "")}

    def create_event(self, slot: Slot, attendees: list[str], title: str) -> EventReceipt:
        if not attendees:
            raise ToolError("create_event: at least one attendee is required")
        created = self._call(
            "event creation",
            self._services["calendar"]
            .events()
            .insert(
                calendarId=self._calendar_id,
                body={
                    "summary": title,
                    "start": {"dateTime": slot["start"], "timeZone": self._timezone},
                    "end": {"dateTime": slot["end"], "timeZone": self._timezone},
                    "attendees": [{"email": person} for person in attendees],
                },
                # The plan says "create the invite", so the invitations go out.
                sendUpdates="all",
            ),
        )
        return {
            "event_id": created["id"],
            "html_link": created.get("htmlLink", ""),
        }

    # --- verification -------------------------------------------------------

    def message_in_sent(self, message_id: str) -> bool:
        """Read the label back. A 200 from send is not proof (LESSON 22)."""
        try:
            message = self._call(
                "send verification",
                self._services["gmail"]
                .users()
                .messages()
                .get(userId="me", id=message_id, format="metadata"),
            )
        except ToolError:
            return False
        return was_sent(message)

    def event_on_calendar(self, event_id: str) -> bool:
        try:
            event = self._call(
                "event verification",
                self._services["calendar"]
                .events()
                .get(calendarId=self._calendar_id, eventId=event_id),
            )
        except ToolError:
            return False
        return event_is_live(event)


def _as_bytes(data: Any) -> bytes:
    """Drive returns bytes for media; be forgiving about a str from a double."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    raise ToolError(f"unexpected download payload of type {type(data).__name__}")
