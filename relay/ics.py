"""Building a calendar invitation as an .ics file.

LESSON 31 - an attachment can be an action.
Without the Calendar API there is no way to write to someone's calendar. But
there is a way to ASK: an .ics file carrying ``METHOD:REQUEST`` is not a
document, it is an invitation. Gmail and Outlook render it with Yes / Maybe /
No buttons, and accepting puts the event on the recipient's own calendar.

So "book the meeting" becomes "send the invitation", which is arguably the
more honest thing for an agent to do anyway - it asks the attendees rather than
writing into their day.

What this CANNOT do, and the reason the Google backend still exists: it cannot
tell you when anyone is free. An invitation proposes a time; free/busy knows
one. See LESSON 32 in local_backend.py.
"""

from __future__ import annotations

import datetime as dt
import uuid

#: RFC 5545 asks for CRLF line endings. Most clients tolerate bare LF, but the
#: ones that do not fail by silently showing the invite as a plain attachment.
CRLF = "\r\n"

#: Lines longer than 75 octets must be folded, or strict parsers reject the
#: file. A long subject or a lot of attendees reaches this quickly.
MAX_LINE = 75


def _fold(line: str) -> str:
    """Fold one content line to 75 octets, continuation lines starting with a space."""
    encoded = line.encode("utf-8")
    if len(encoded) <= MAX_LINE:
        return line
    chunks = []
    start = 0
    while start < len(encoded):
        end = min(start + (MAX_LINE if not chunks else MAX_LINE - 1), len(encoded))
        # Do not split a multi-byte character in half.
        while end > start and (encoded[end - 1] & 0xC0) == 0x80:
            end -= 1
        chunk = encoded[start:end].decode("utf-8")
        chunks.append(chunk if not chunks else " " + chunk)
        start = end
    return CRLF.join(chunks)


def _escape(value: str) -> str:
    """Escape the four characters that are special inside a property value."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _utc(moment: dt.datetime) -> str:
    """iCalendar UTC stamp. Everything is sent as UTC to dodge VTIMEZONE blocks."""
    if moment.tzinfo is None:
        raise ValueError("refusing to write a naive datetime into an invitation")
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_invite(
    start: dt.datetime,
    end: dt.datetime,
    title: str,
    organiser: str,
    attendees: list[str],
    description: str = "",
    uid: str | None = None,
    method: str = "REQUEST",
) -> str:
    """An .ics invitation for one event.

    ``uid`` is stable per event: send the same UID again with ``METHOD:CANCEL``
    and clients treat it as cancelling that meeting rather than as a new one.
    """
    if end <= start:
        raise ValueError("an event must end after it starts")
    if not attendees:
        raise ValueError("an invitation needs at least one attendee")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RELAY//Goal to action//EN",
        "CALSCALE:GREGORIAN",
        # The line that turns a file into an invitation.
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid or uuid.uuid4()}",
        f"DTSTAMP:{_utc(dt.datetime.now(dt.timezone.utc))}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{_escape(title)}",
        f"ORGANIZER:mailto:{organiser}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    for person in attendees:
        # RSVP=TRUE is what makes the client show accept/decline buttons.
        lines.append(
            f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{person}"
        )
    lines += ["STATUS:CONFIRMED", "SEQUENCE:0", "END:VEVENT", "END:VCALENDAR"]

    return CRLF.join(_fold(line) for line in lines) + CRLF


def parse_slot(slot: dict) -> tuple[dt.datetime, dt.datetime]:
    """Turn a Slot's ISO strings back into datetimes."""
    return (
        dt.datetime.fromisoformat(str(slot["start"])),
        dt.datetime.fromisoformat(str(slot["end"])),
    )
