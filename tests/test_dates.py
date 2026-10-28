"""Date resolution. Pure arithmetic, so every case has one right answer."""

from __future__ import annotations

import datetime as dt

import pytest

from relay import dates

# Tue 27 Oct 2026, the date on the build plan.
TUE = dt.date(2026, 10, 27)
SAT = dt.date(2026, 10, 31)
SUN = dt.date(2026, 11, 1)
MON = dt.date(2026, 11, 2)
FRI = dt.date(2026, 11, 6)


def test_next_week_from_the_example_goal():
    """The plan's own example: 'next week' on Tue 27 Oct is Mon 2 - Fri 6 Nov 2026."""
    assert dates.resolve_window("next week", TUE) == (MON, FRI)


def test_next_week_is_mon_to_fri_not_seven_days():
    start, end = dates.resolve_window("next week", TUE)
    assert start.weekday() == 0 and end.weekday() == 4
    assert (end - start).days == 4


@pytest.mark.parametrize("today", [MON, dt.date(2026, 11, 4), FRI])
def test_next_week_is_stable_across_the_whole_week(today):
    """Asking on Monday or Friday of one week must give the same next week."""
    assert dates.resolve_window("next week", today) == (
        dt.date(2026, 11, 9),
        dt.date(2026, 11, 13),
    )


def test_next_week_on_a_saturday_is_the_coming_week():
    assert dates.resolve_window("next week", SAT) == (MON, FRI)


def test_next_week_on_a_sunday_is_the_coming_week_too():
    """The off-by-one trap: ISO weeks end on Sunday, so Sunday's 'next week'
    must still be the Monday two days away, not nine."""
    assert dates.resolve_window("next week", SUN) == (MON, FRI)


def test_this_week_is_the_rest_of_the_week():
    assert dates.resolve_window("this week", TUE) == (TUE, dt.date(2026, 10, 30))


def test_this_week_never_offers_the_past():
    start, _ = dates.resolve_window("this week", dt.date(2026, 10, 29))
    assert start == dt.date(2026, 10, 29)


def test_this_week_on_a_weekend_rolls_forward():
    """There is no usable rest-of-week on Saturday, so roll to the coming week."""
    assert dates.resolve_window("this week", SAT) == (MON, FRI)
    assert dates.resolve_window("this week", SUN) == (MON, FRI)


def test_tomorrow_and_today():
    assert dates.resolve_window("tomorrow", TUE) == (dt.date(2026, 10, 28),) * 2
    assert dates.resolve_window("today", TUE) == (TUE, TUE)


def test_next_n_days():
    assert dates.resolve_window("next 3 days", TUE) == (TUE, dt.date(2026, 10, 30))


def test_absurd_range_is_refused():
    with pytest.raises(dates.DateRangeError):
        dates.resolve_window("next 900 days", TUE)


def test_unrecognised_phrase_raises_rather_than_guessing():
    with pytest.raises(dates.DateRangeError):
        dates.resolve_window("sometime after the audit", TUE)


def test_phrase_is_found_inside_a_full_goal():
    goal = "Email Rahul the Q3 report and set a 30-minute review with him and Anu next week."
    assert dates.resolve_window(goal, TUE) == (MON, FRI)


def test_describe_shows_real_dates():
    """The whole point of resolving: the card says dates, not 'next week'."""
    assert dates.describe(MON, FRI) == "Mon 02 Nov 2026 to Fri 06 Nov 2026"
    assert dates.describe(MON, MON) == "Mon 02 Nov 2026"
