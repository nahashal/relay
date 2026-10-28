"""Turning "next week" into two dates, in the user's timezone.

LESSON 8 - do not let the model do arithmetic you can do exactly.
Asking a language model what dates "next week" covers invites a confident wrong
answer, and the error is invisible on the approval card ("Tue 10 Nov" looks just
as plausible as "Tue 3 Nov"). Date resolution is pure arithmetic with a known
answer, so it belongs in Python, tested, and the model is only asked which
*phrase* the user used.

House rules, chosen once so the approval card is never ambiguous:
  * weeks run Monday to Friday - RELAY does not offer weekend meetings
  * "next week" means the Mon-Fri after the current week, even on a Sunday
  * "this week" means the rest of this week: today (or Monday, on a weekend)
    through Friday, because offering a slot in the past is never useful
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"


class DateRangeError(ValueError):
    """The phrase could not be resolved to a window. Ask, do not guess."""


def today_in(timezone: str = DEFAULT_TZ) -> dt.date:
    """Today according to the user's clock, not the server's."""
    return dt.datetime.now(ZoneInfo(timezone)).date()


def _monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def next_week(today: dt.date) -> tuple[dt.date, dt.date]:
    """The Mon-Fri block after the week containing ``today``."""
    monday = _monday_of(today) + dt.timedelta(days=7)
    return monday, monday + dt.timedelta(days=4)


def this_week(today: dt.date) -> tuple[dt.date, dt.date]:
    """Today through Friday. On a weekend, the coming Mon-Fri instead.

    A "this week" that has already ended is not a window; rolling forward is the
    only reading that can produce a bookable slot.
    """
    if today.weekday() >= 5:  # Sat or Sun: there is no usable rest-of-week
        # ISO weeks run Mon-Sun, so _monday_of(Saturday) and _monday_of(Sunday)
        # both point at the Monday just gone; next_week therefore lands on the
        # coming Monday for either day. No special case needed.
        return next_week(today)
    return today, _monday_of(today) + dt.timedelta(days=4)


#: Weekday names, long and short. "Thurs" and "Tues" are common enough to list.
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

#: Phrases RELAY will resolve on its own. Anything else becomes a question.
#: Order matters: "next week" must be tried before the weekday pattern, or
#: "next Wednesday" would match "wed" inside "Wednesday" first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bnext week\b", re.I), "next_week"),
    (re.compile(r"\bthis week\b", re.I), "this_week"),
    (re.compile(r"\btomorrow\b", re.I), "tomorrow"),
    (re.compile(r"\btoday\b", re.I), "today"),
    (re.compile(r"\bnext (\d+) days\b", re.I), "next_n_days"),
    # "on Thursday", "next Thursday", "Thurs" - the single commonest way people
    # name a day, and the one the first version could only answer with a question.
    (
        re.compile(
            r"\b(?:on\s+|next\s+|this\s+)?(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b",
            re.I,
        ),
        "weekday",
    ),
]


def next_weekday(today: dt.date, weekday: int, *, force_next_week: bool = False) -> dt.date:
    """The next date falling on ``weekday``.

    A bare "Thursday" is the coming Thursday, always in the future: asking on a
    Thursday for "Thursday" means the next one, not the hours you are already in.

    "next Thursday" is genuinely ambiguous in English - some people mean the
    Thursday coming, others the Thursday of next week. RELAY takes the second
    reading, because it already defines "next week" as the Mon-Fri block after
    this one, and "next Thursday" landing outside "next week" would be
    incoherent. The approval card always shows the resolved date, so a reader
    who meant the other thing can see it and edit.
    """
    if force_next_week:
        monday_next = _monday_of(today) + dt.timedelta(days=7)
        return monday_next + dt.timedelta(days=weekday)
    ahead = (weekday - today.weekday()) % 7
    return today + dt.timedelta(days=ahead or 7)


#: "2pm", "2 pm", "14:00", "2:30pm", "9am". Deliberately small: anything
#: this does not match is treated as "no time given", which falls back to the
#: old behaviour rather than guessing an hour.
_CLOCK = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(?:at\s+)?(\d{1,2}):(\d{2})\b",
    re.I,
)


def resolve_time(phrase: str) -> dt.time | None:
    """The clock time named in a phrase, or None if it names none.

    None is not a failure: most goals give a day and no hour, and that case
    still goes through the normal path.
    """
    match = _CLOCK.search(phrase or "")
    if not match:
        return None

    if match.group(3):  # the am/pm form
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if hour == 12:
            hour = 0
        if match.group(3).lower() == "pm":
            hour += 12
    else:  # the 24-hour form
        hour = int(match.group(4))
        minute = int(match.group(5))

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt.time(hour, minute)


def resolve_window(
    phrase: str, today: dt.date | None = None, timezone: str = DEFAULT_TZ
) -> tuple[dt.date, dt.date]:
    """Resolve a relative phrase to an inclusive ``(start, end)`` pair.

    Raises ``DateRangeError`` for anything unrecognised, which the planner turns
    into an open question rather than a guess.
    """
    today = today or today_in(timezone)

    for pattern, kind in _PATTERNS:
        match = pattern.search(phrase)
        if not match:
            continue
        match kind:
            case "next_week":
                return next_week(today)
            case "this_week":
                return this_week(today)
            case "tomorrow":
                day = today + dt.timedelta(days=1)
                return day, day
            case "today":
                return today, today
            case "next_n_days":
                days = int(match.group(1))
                if not 1 <= days <= 60:
                    raise DateRangeError(f"{days} days is outside the supported range")
                return today, today + dt.timedelta(days=days)
            case "weekday":
                name = match.group(1).lower()
                # "next Thursday" means the week after, when today is before it;
                # a bare "Thursday" means the coming one.
                explicit_next = bool(
                    re.search(rf"\bnext\s+{re.escape(name)}\b", phrase, re.I)
                )
                day = next_weekday(today, _WEEKDAYS[name], force_next_week=explicit_next)
                if day.weekday() >= 5:
                    raise DateRangeError(
                        f"{day.strftime('%A')} is a weekend; RELAY only offers weekdays"
                    )
                return day, day

    raise DateRangeError(f"could not resolve a date window from {phrase!r}")


def describe(start: dt.date, end: dt.date) -> str:
    """Human wording for the approval card, so the user sees real dates.

    This is the whole reason resolution is explicit: the card says
    "Mon 2 to Fri 6 Nov 2026", not "next week".
    """
    # %d zero-pads, which is slightly ugly but portable; %-d is glibc-only and
    # raises on Windows, and a crash while rendering an approval card is a far
    # worse trade than "Mon 02 Nov".
    fmt = "%a %d %b %Y"
    if start == end:
        return start.strftime(fmt)
    return f"{start.strftime(fmt)} to {end.strftime(fmt)}"
