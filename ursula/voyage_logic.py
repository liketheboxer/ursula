"""Gatherings (internally voyages): parsing, reminders, repeats and the RSVP card, apart from Discord."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord


def is_weekday_name(text: str) -> bool:
    t = text.strip().lower().rstrip(".")
    return any(t in (n, n[:3]) for n in WEEKDAYS)


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DEFAULT_REMINDERS = "1d, 1h"
REMINDER_PRESETS = [
    ("1 day and 1 hour before (default)", "1d, 1h"),
    ("1 hour before", "1h"),
    ("2 days, 1 day and 1 hour before", "2d, 1d, 1h"),
    ("30 minutes before", "30m"),
    ("No reminders before the start", "none"),
]
REPEATS = {"none": "Doesn't repeat", "weekly": "Every week", "biweekly": "Every 2 weeks", "monthly": "Every month"}
# 1.5.0: more patterns. A repeat is stored as a short code:
#   none | weekly | biweekly | weeks:N (every N weeks, 1-12) | monthly (the same date)
#   | nth:K:D (the Kth weekday D of each month; K 1-4, or -1 for the last; D 0=Monday .. 6=Sunday)
# The two "nth" choices below are filled in from the voyage's own date when it's saved.
REPEAT_CHOICES = [
    ("Doesn't repeat", "none"), ("Every week", "weeks:1"), ("Every 2 weeks", "weeks:2"),
    ("Every 3 weeks", "weeks:3"), ("Every 4 weeks", "weeks:4"), ("Every month, same date", "monthly"),
    ("Every month, same weekday (like the 2nd Saturday)", "nth"),
    ("Every month, last weekday (like the last Friday)", "nth:last"),
]
MAX_REPEAT_WEEKS = 12
MAX_SKIPS = 20
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", -1: "last"}
MAX_REMINDERS = 5


class ParseError(ValueError):
    """Something a member typed that we couldn't understand; the message is shown to them."""


# ------------------------------------------------------------ dates and times
def parse_date(text: str, today: date) -> date:
    t = text.strip().lower()
    if t in ("today", "tonight"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if t in (name, name[:3], name[:3] + "."):
            ahead = (i - today.weekday()) % 7
            return today + timedelta(days=ahead)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return _make_date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", t)
    if m:
        month, day = int(m[1]), int(m[2])
        if m[3]:
            year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return _make_date(year, month, day)
        d = _make_date(today.year, month, day)
        return d if d >= today else _make_date(today.year + 1, month, day)
    raise ParseError(f"I couldn't read the date \"{text}\". Try friday, tomorrow, 10/3 or 2026-10-03.")


def _make_date(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError as e:
        raise ParseError(f"{y}-{m:02d}-{d:02d} isn't a real date.") from e


def parse_time(text: str) -> time:
    t = text.strip().lower().replace(" ", "").replace(".", "")
    if t == "noon":
        return time(12, 0)
    if t == "midnight":
        return time(0, 0)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm|a|p)?", t)
    if not m:
        raise ParseError(f"I couldn't read the time \"{text}\". Try 8pm, 8:30pm or 20:30.")
    hour, minute, suffix = int(m[1]), int(m[2] or 0), m[3]
    if minute > 59:
        raise ParseError(f"\"{text}\" has too many minutes.")
    if suffix:
        if not 1 <= hour <= 12:
            raise ParseError(f"\"{text}\" isn't a 12-hour time.")
        hour = hour % 12 + (12 if suffix.startswith("p") else 0)
    elif hour > 23:
        raise ParseError(f"\"{text}\" isn't a time of day.")
    return time(hour, minute)


# Short names people type after a time ("8pm ET"). Summer/winter spellings map to the same zone,
# which then applies whichever offset is right on that date.
ZONE_ALIASES = {
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver", "mountain": "America/Denver",
    "az": "America/Phoenix", "arizona": "America/Phoenix",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago", "central": "America/Chicago",
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York", "eastern": "America/New_York",
    "akt": "America/Anchorage", "akst": "America/Anchorage", "akdt": "America/Anchorage",
    "ht": "Pacific/Honolulu", "hst": "Pacific/Honolulu",
    "at": "America/Halifax", "ast": "America/Halifax", "adt": "America/Halifax",
    "utc": "UTC", "gmt": "UTC", "z": "UTC",
    "uk": "Europe/London", "bst": "Europe/London",
    "cet": "Europe/Berlin", "cest": "Europe/Berlin",
    "eet": "Europe/Athens", "eest": "Europe/Athens",
    "ist": "Asia/Kolkata", "jst": "Asia/Tokyo",
    "aest": "Australia/Sydney", "aedt": "Australia/Sydney", "acst": "Australia/Adelaide",
    "awst": "Australia/Perth", "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland",
}


def zone_from_name(name: str) -> ZoneInfo | None:
    """A zone from a short name (ET, PST, UTC) or a full one (America/New_York)."""
    from zoneinfo import ZoneInfoNotFoundError
    key = ZONE_ALIASES.get(name.strip().lower(), name.strip())
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def split_zone(text: str) -> tuple[str, ZoneInfo | None]:
    """ "8pm ET" -> ("8pm", New York); "20:00 Europe/London" -> ("20:00", London); "8pm" -> ("8pm", None)."""
    parts = text.strip().rsplit(" ", 1)
    if len(parts) == 2:
        tz = zone_from_name(parts[1])
        if tz is not None:
            return parts[0], tz
        if re.fullmatch(r"[A-Za-z_/]{2,}", parts[1]) and parts[1].lower() not in ("am", "pm"):
            raise ParseError(f"I don't know the time zone \"{parts[1]}\". Try ET, PT, UTC or a name like "
                             "Europe/London, or set yours with /timezone set.")
    return text, None


def zone_label(tz: ZoneInfo, when: datetime) -> str:
    """ "Pacific (PDT)" style label for replies."""
    abbr = when.astimezone(tz).strftime("%Z")
    return f"{tz.key} ({abbr})" if abbr and abbr != tz.key else tz.key


def to_utc(day: date, at: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz).astimezone(timezone.utc)


# ------------------------------------------------------------ reminders
def parse_reminders(text: str | None) -> list[int]:
    """Minutes before the start, largest first. "1d, 1h, 15m" -> [1440, 60, 15]; "none" -> []."""
    if text is None:
        text = DEFAULT_REMINDERS
    t = text.strip().lower()
    if t in ("", "none", "off", "no"):
        return []
    out: set[int] = set()
    for token in re.split(r"[,\s]+", t):
        if not token or token in ("start", "and"):
            continue  # the start ping always happens when the voice channel opens
        m = re.fullmatch(r"(\d+)(d|h|m)", token)
        if not m:
            raise ParseError(f"I couldn't read the reminder \"{token}\". Use things like 2d, 3h or 15m.")
        n = int(m[1]) * {"d": 1440, "h": 60, "m": 1}[m[2]]
        if not 5 <= n <= 14 * 1440:
            raise ParseError("Reminders can be from 5 minutes to 14 days before the start.")
        out.add(n)
    if len(out) > MAX_REMINDERS:
        raise ParseError(f"That's a lot of pinging! Up to {MAX_REMINDERS} reminders, please.")
    return sorted(out, reverse=True)


def format_reminders(minutes: list[int]) -> str:
    if not minutes:
        return "none before the start"
    parts = []
    for n in minutes:
        if n % 1440 == 0:
            parts.append(f"{n // 1440} day{'s' if n >= 2880 else ''}")
        elif n % 60 == 0:
            parts.append(f"{n // 60} hour{'s' if n >= 120 else ''}")
        else:
            parts.append(f"{n} min")
    return ", ".join(parts) + " before"


def due_reminder(starts_at: datetime, created_at: datetime, minutes: list[int], sent: list[int],
                 now: datetime) -> int | None:
    """The reminder to send now, if any. Reminders whose time had already passed when the voyage was
    created are skipped rather than fired late; if several are due, only the closest to the start goes."""
    due = [m for m in minutes if m not in sent and now >= starts_at - timedelta(minutes=m)
           and starts_at - timedelta(minutes=m) >= created_at and now < starts_at]
    return min(due) if due else None


def overdue_reminders(starts_at: datetime, created_at: datetime, minutes: list[int], sent: list[int],
                      now: datetime) -> list[int]:
    """Reminders that will never go out (passed before creation, or superseded); mark them sent."""
    return [m for m in minutes if m not in sent and starts_at - timedelta(minutes=m) < created_at]


# ------------------------------------------------------------ repeats
def _weeks(code: str) -> int | None:
    if code == "weekly":
        return 1
    if code == "biweekly":
        return 2
    m = re.fullmatch(r"weeks:(\d{1,2})", code or "")
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= MAX_REPEAT_WEEKS else None


def _nth(code: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"nth:(-1|[1-4]):([0-6])", code or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def valid_repeat(code: str) -> bool:
    return code in ("none", "monthly") or _weeks(code) is not None or _nth(code) is not None


def nth_of(day: date, last: bool = False) -> str:
    """The "nth weekday" code for a date: 2026-10-10 is the 2nd Saturday, nth:2:5.
    The 5th of a weekday (the 29th to 31st) only happens some months, so it's taken as the last."""
    k = (day.day - 1) // 7 + 1
    return f"nth:{-1 if last or k == 5 else k}:{day.weekday()}"


def resolve_repeat(choice: str, day: date) -> str:
    """Turn a picked choice into the stored code, using the voyage's date for the monthly-weekday ones."""
    choice = (choice or "none").strip().lower()
    if choice == "nth":
        return nth_of(day)
    if choice == "nth:last":
        return nth_of(day, last=True)
    if choice in ("weekly", "biweekly"):
        return choice
    if valid_repeat(choice):
        return choice
    m = re.fullmatch(r"every\s+(\d{1,2})\s+weeks?", choice)
    if m and 1 <= int(m.group(1)) <= MAX_REPEAT_WEEKS:
        return f"weeks:{int(m.group(1))}"
    raise ParseError(f"I don't know that repeat. Pick one from the list, or say \"every 3 weeks\" "
                     f"(up to {MAX_REPEAT_WEEKS}).")


def describe_repeat(code: str) -> str:
    if code in REPEATS:
        return REPEATS[code]
    n = _weeks(code)
    if n is not None:
        return "Every week" if n == 1 else f"Every {n} weeks"
    nth = _nth(code)
    if nth is not None:
        return f"Every month on the {ORDINALS[nth[0]]} {WEEKDAYS[nth[1]].capitalize()}"
    return code


def repeat_choices(day: date | None) -> list[tuple[str, str]]:
    """The choices with the monthly-weekday ones spelled out for a known date."""
    if day is None:
        return REPEAT_CHOICES
    out = []
    for label, code in REPEAT_CHOICES:
        if code == "monthly":
            label = f"Every month on the {day.day}{_suffix(day.day)}"
        elif code in ("nth", "nth:last"):
            code = resolve_repeat(code, day)
            label = describe_repeat(code)
            if any(c == code for _, c in out):
                continue
        out.append((label, code))
    return out


def _suffix(n: int) -> str:
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _nth_day(y: int, m: int, k: int, wd: int) -> date:
    if k == -1:
        last = date(y, m, calendar.monthrange(y, m)[1])
        return last - timedelta(days=(last.weekday() - wd) % 7)
    first = date(y, m, 1)
    return first + timedelta(days=(wd - first.weekday()) % 7 + 7 * (k - 1))


def next_occurrence(starts_at: datetime, repeat: str, tz: ZoneInfo, anchor_day: int | None = None,
                    at: time | None = None) -> datetime | None:
    """The next start in a series, keeping the same local wall-clock time across daylight saving.
    Monthly repeats aim for anchor_day (the series' original day) so Jan 31 -> Feb 28 -> Mar 31.
    `at` is the series' own time of day, so a start pushed on by a daylight-saving gap (2:30 becoming
    3:30) doesn't drag the rest of the series with it."""
    local = starts_at.astimezone(tz)
    weeks, nth = _weeks(repeat), _nth(repeat)
    if weeks is not None:
        nxt = local.date() + timedelta(days=7 * weeks)
    elif repeat == "monthly" or nth is not None:
        y, m = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        if nth is not None:
            nxt = _nth_day(y, m, *nth)
        else:
            nxt = date(y, m, min(anchor_day or local.day, calendar.monthrange(y, m)[1]))
    else:
        return None
    return to_utc(nxt, at or local.timetz().replace(tzinfo=None), tz)


def parse_skips(text: str | None) -> list[date]:
    out = []
    for part in (text or "").split(","):
        try:
            out.append(date.fromisoformat(part.strip()))
        except ValueError:
            continue
    return sorted(set(out))


def format_skips(days: list[date], after: date | None = None) -> str:
    """Stored form, dropping dates already gone by and keeping the list short."""
    kept = sorted({d for d in days if after is None or d >= after})
    return ",".join(d.isoformat() for d in kept[:MAX_SKIPS])


def following(starts_at: datetime, repeat: str, tz: ZoneInfo, anchor_day: int | None = None,
              until: date | None = None, skips: list[date] | None = None, after: datetime | None = None,
              at: time | None = None) -> datetime | None:
    """The next start of a series that isn't skipped, isn't past its end date and is after `after`."""
    skips = set(skips or [])
    nxt = next_occurrence(starts_at, repeat, tz, anchor_day, at)
    for _ in range(600):  # every week for over ten years; a series can't run forever looking
        if nxt is None:
            return None
        day = nxt.astimezone(tz).date()
        if until is not None and day > until:
            return None
        if day not in skips and (after is None or nxt > after):
            return nxt
        nxt = next_occurrence(nxt, repeat, tz, anchor_day, at)
    return None


def upcoming_dates(starts_at: datetime, repeat: str, tz: ZoneInfo, anchor_day: int | None = None,
                   until: date | None = None, count: int = 8, at: time | None = None) -> list[datetime]:
    """The series' next starts after this one, skipped dates included (so they can be shown and un-skipped)."""
    out: list[datetime] = []
    nxt = next_occurrence(starts_at, repeat, tz, anchor_day, at)
    while nxt is not None and len(out) < count:
        if until is not None and nxt.astimezone(tz).date() > until:
            break
        out.append(nxt)
        nxt = next_occurrence(nxt, repeat, tz, anchor_day, at)
    return out


# ------------------------------------------------------------ RSVPs
@dataclass
class Rsvps:
    aboard: list[int] = field(default_factory=list)   # in order of joining
    maybe: list[int] = field(default_factory=list)
    cant: list[int] = field(default_factory=list)
    waitlist: list[int] = field(default_factory=list)  # in order of joining

    def of(self, user_id: int) -> str | None:
        for status in ("aboard", "maybe", "cant", "waitlist"):
            if user_id in getattr(self, status):
                return status
        return None


def placement(wanted: str, rsvps: Rsvps, capacity: int | None) -> str:
    """Where a member lands when they press a button: a full voyage puts "aboard" on the waitlist."""
    if wanted != "aboard":
        return wanted
    if capacity is None or len(rsvps.aboard) < capacity:
        return "aboard"
    return "waitlist"


# ------------------------------------------------------------ the card
STATUS_TEXT = {"scheduled": "On the board", "started": "Under way", "ended": "Over", "cancelled": "Called off"}
STATUS_COLOUR = {
    "scheduled": discord.Colour.from_rgb(201, 140, 58),    # Anarres ochre
    "started": discord.Colour.from_rgb(122, 168, 196),     # pale sky
    "ended": discord.Colour.dark_grey(),
    "cancelled": discord.Colour.dark_grey(),
}


def _names(ids: list[int], limit: int = 900) -> str:
    shown: list[str] = []
    for i in ids:
        mention = f"<@{i}>"
        if len(", ".join(shown + [mention])) > limit:
            return ", ".join(shown) + f" and {len(ids) - len(shown)} more"
        shown.append(mention)
    return ", ".join(shown) or "Nobody yet"


def render_voyage(v, rsvps: Rsvps, profile=None, emoji: str = "", crew_link: str | None = None) -> discord.Embed:
    """A gathering's card. `profile` and `crew_link` are PlunderBot leftovers, kept so callers needn't change."""
    stamp = int(datetime.fromisoformat(v.starts_at).timestamp())
    title = f"{emoji} {v.title}".strip()
    embed = discord.Embed(title=title, colour=STATUS_COLOUR.get(v.status, discord.Colour.default()))
    if v.description:
        embed.description = v.description[:2000]
    when = f"<t:{stamp}:F> (<t:{stamp}:R>)"
    if v.repeat != "none":
        when += f"\n{describe_repeat(v.repeat)}"
        if getattr(v, "repeat_until", None):
            when += f" until {date.fromisoformat(v.repeat_until).strftime('%b %-d, %Y')}"
    embed.add_field(name="When", value=when, inline=False)
    place = getattr(v, "place", None)
    if place:
        embed.add_field(name="Where", value=place[:100], inline=False)
    embed.add_field(name="Called by", value=f"<@{v.organizer_id}>", inline=True)
    role_id = getattr(v, "notify_role_id", None)
    if role_id:
        embed.add_field(name="For", value=f"<@&{role_id}>", inline=True)
    embed.add_field(name="Status", value=STATUS_TEXT.get(v.status, v.status), inline=True)
    cap = f" ({len(rsvps.aboard)}/{v.capacity})" if v.capacity else f" ({len(rsvps.aboard)})"
    embed.add_field(name=f"Going{cap}", value=_names(rsvps.aboard), inline=False)
    if rsvps.waitlist:
        embed.add_field(name=f"Waitlist ({len(rsvps.waitlist)})", value=_names(rsvps.waitlist), inline=False)
    embed.add_field(name=f"Maybe ({len(rsvps.maybe)})", value=_names(rsvps.maybe), inline=False)
    if rsvps.cant:
        embed.add_field(name="Can't make it", value=str(len(rsvps.cant)), inline=True)
    if v.status == "scheduled":
        embed.set_footer(text=f"Reminders: {format_reminders(v.reminder_minutes)}. "
                              "Everyone Going or Maybe gets a ping at the start.")
    return embed
