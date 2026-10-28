"""Finding a time when everyone is free, from busy intervals.

LESSON 20 - the shared half of two backends must be one function.
MockBackend and GoogleBackend both have to answer "when are these three people
free for 30 minutes next week", and if each did its own arithmetic they would
eventually disagree - the demo offering a slot the live run rejects, or worse,
the other way round. So the only thing a backend is responsible for is
*fetching busy intervals*. Turning those into offerable slots happens here,
once, in a pure function with no network and no clock.

That also means every slot test already written against the mock is really a
test of the code the live backend runs.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .tools.base import Slot

#: Slot search granularity. 30 minutes matches the example goal; a smaller step
#: finds more slots at the cost of a longer list to choose from.
STEP = dt.timedelta(minutes=30)


def free_slots(
    busy: list[tuple[dt.datetime, dt.datetime]],
    minutes: int,
    window_start: dt.date,
    window_end: dt.date,
    timezone: str,
    day_start: str = "09:00",
    day_end: str = "18:00",
    step: dt.timedelta = STEP,
) -> list[Slot]:
    """Every ``minutes``-long slot inside working hours that collides with nothing.

    ``busy`` is the union of everyone's busy intervals - who owns which interval
    does not matter, because a slot is only offerable if it clears all of them.

    Deliberately a linear walk rather than an interval sweep: a five-day window
    is a few hundred candidates, and being obviously correct matters more here
    than being fast. Anyone reading this needs to believe it.
    """
    if minutes <= 0:
        raise ValueError("minutes must be positive")

    zone = ZoneInfo(timezone)
    duration = dt.timedelta(minutes=minutes)
    # Normalise to one timezone before comparing: Google returns UTC, the mock
    # returns local offsets, and a naive/aware mix raises at comparison time.
    normalised = [(start.astimezone(zone), end.astimezone(zone)) for start, end in busy]

    found: list[Slot] = []
    day = window_start
    while day <= window_end:
        if day.weekday() < 5:  # weekends are not offered
            opens = _at(day, day_start, zone)
            closes = _at(day, day_end, zone)
            cursor = opens
            while cursor + duration <= closes:
                finish = cursor + duration
                # Half-open overlap test: touching edges do not collide, so a
                # meeting may start exactly when another ends.
                clash = any(cursor < b_end and b_start < finish for b_start, b_end in normalised)
                if not clash:
                    found.append({"start": cursor.isoformat(), "end": finish.isoformat()})
                cursor += step
        day += dt.timedelta(days=1)
    return found


def _at(day: dt.date, clock: str, zone: ZoneInfo) -> dt.datetime:
    """A wall-clock time on a given day, in a given zone.

    Built from parts rather than parsed from a string with a fixed offset: an
    offset that is right in November is wrong in July, and a meeting booked an
    hour out is exactly the kind of error nobody notices on an approval card.
    """
    hour, minute = (int(part) for part in clock.split(":", 1))
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
