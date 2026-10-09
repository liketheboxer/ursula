"""Birthdays (Ursula 1.5.0, from PlunderBot): the date rules, kept free of Discord so they're easy to test.

Only a month and a day are ever stored, never a year, so nobody's age is on file.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta


def valid_date(month: int, day: int) -> bool:
    """Month and day exist in some year (February 29 counts)."""
    if not 1 <= month <= 12:
        return False
    return 1 <= day <= calendar.monthrange(2024, month)[1]  # 2024 is a leap year


def is_birthday_on(month: int, day: int, today: date) -> bool:
    """February 29 birthdays are celebrated on February 28 in non-leap years."""
    if (month, day) == (today.month, today.day):
        return True
    return (month, day) == (2, 29) and not calendar.isleap(today.year) and (today.month, today.day) == (2, 28)


def next_occurrence(month: int, day: int, today: date) -> date:
    """The next date (today or later) this birthday is celebrated."""
    for year in (today.year, today.year + 1, today.year + 2):
        if (month, day) == (2, 29) and not calendar.isleap(year):
            candidate = date(year, 2, 28)
        else:
            candidate = date(year, month, day)
        if candidate >= today:
            return candidate
    raise ValueError("unreachable")  # pragma: no cover


def upcoming(entries: list[tuple[int, int, int]], today: date, limit: int = 10) -> list[tuple[int, date]]:
    """(user_id, date) for the next `limit` birthdays, soonest first. Ties sort by user id."""
    dated = [(uid, next_occurrence(m, d, today)) for uid, m, d in entries]
    dated.sort(key=lambda x: (x[1], x[0]))
    return dated[:limit]


CHANGE_COOLDOWN = timedelta(days=30)
TYPO_GRACE = timedelta(hours=1)


def change_allowed(last_change: datetime | None, now: datetime) -> tuple[bool, bool, datetime | None]:
    """Whether a member may change their saved birthday now.

    Returns (allowed, starts_new_window, locked_until). A change opens a 30-day window; for
    the first hour of it further changes are free (typo fixes) and don't extend it. After
    that the date is locked until the window ends, so nobody can collect a toast every day.
    """
    if last_change is None or now - last_change >= CHANGE_COOLDOWN:
        return True, True, None
    if now - last_change <= TYPO_GRACE:
        return True, False, None
    return False, False, last_change + CHANGE_COOLDOWN


def due_to_announce(now_local: datetime, hour: int, last_announced: str | None) -> bool:
    """True once the set hour has arrived and today's announcement hasn't gone out yet.

    Checking "at or after the hour" rather than "during the hour" means a bot that was
    restarted or offline at the set time still announces later that day.
    """
    today = now_local.date().isoformat()
    # ">=" rather than "!=": if the server's time zone is moved west, the local date can step
    # back a day, and an announcement already sent for a later date must not be sent again.
    return now_local.hour >= hour and (last_announced is None or last_announced < today)
