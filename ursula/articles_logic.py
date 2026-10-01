"""Customs (internally "articles", from PlunderBot): the server's own if-this-then-that rules.

A rule (a custom) has one trigger, a list of actions, and limits (where it works, a cooldown,
a chance). Pure rules live here: matching words, reading schedules, filling in reply templates,
and which actions make sense for which trigger. The cog does the Discord plumbing.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .voyage_logic import ParseError, parse_time

# Triggers
KEYWORD, JOIN, LEAVE, ROLE_ADDED, ROLE_REMOVED, BOOST, REACTION, SCHEDULE = (
    "keyword", "join", "leave", "role_added", "role_removed", "boost", "reaction", "schedule")
TRIGGERS = {
    KEYWORD: "Someone says a word or phrase",
    JOIN: "A member joins",
    LEAVE: "A member leaves",
    ROLE_ADDED: "A member gets a role",
    ROLE_REMOVED: "A member loses a role",
    BOOST: "A member boosts the server",
    REACTION: "A message gets an emoji reaction",
    SCHEDULE: "On a schedule",
}
MESSAGE_TRIGGERS = {KEYWORD, REACTION}  # the ones with a message to reply to, react to, pin or repost
MEMBER_TRIGGERS = {KEYWORD, JOIN, LEAVE, ROLE_ADDED, ROLE_REMOVED, BOOST, REACTION}  # the ones with a member

# Matching keywords
MATCHES = {
    "word": "Whole word or phrase, anywhere in the message",
    "contains": "Anywhere, even inside other words",
    "exact": "The whole message, nothing else",
    "starts": "The start of the message",
}

# Actions
REPLY, REACT, ROLE, COUNT, REPOST, PIN = "reply", "react", "role", "count", "repost", "pin"
ACTIONS = {
    REPLY: "Reply or post a message (picked at random from a list)",
    REACT: "React with an emoji",
    ROLE: "Give or take a role",
    COUNT: "Count it (per member or server-wide) for {count}",
    REPOST: "Repost the message in another channel",
    PIN: "Pin the message",
}
COOLDOWN_SCOPES = {"channel": "per channel", "member": "per member", "server": "server-wide"}

MAX_ACTIONS = 8
MAX_REPLIES = 25
MAX_KEYWORDS = 25
MIN_EVERY = timedelta(minutes=15)
PLACEHOLDERS = ("{member}", "{name}", "{count}", "{server}", "{channel}", "{role}", "{cuss}", "{aphorism}")


@dataclass
class Article:
    id: int
    guild_id: int
    name: str
    enabled: int
    trigger: str
    value: str | None          # keywords (comma separated), an emoji, a role id or a schedule
    match: str                  # keyword matching
    threshold: int              # reactions needed
    channels: str               # JSON list of channel ids it works in; [] means everywhere
    only_role_id: int | None    # only members with this role set it off
    cooldown: int               # seconds; 0 for none
    cooldown_scope: str
    chance: int                 # percent
    actions: str                # JSON list of actions
    created_by: int
    created_at: str
    next_run: str | None = None

    @property
    def action_list(self) -> list[dict]:
        try:
            data = json.loads(self.actions or "[]")
        except ValueError:
            return []
        return [a for a in data if isinstance(a, dict) and a.get("type") in ACTIONS]

    @property
    def channel_ids(self) -> list[int]:
        try:
            return [int(c) for c in json.loads(self.channels or "[]")]
        except (ValueError, TypeError):
            return []


# ------------------------------------------------------------ names and keywords
def clean_name(text: str) -> str:
    """Custom names are short and case-insensitive: 'Bruh', 'golden cannonball'."""
    return " ".join((text or "").replace("`", "'").split())[:40]


def split_keywords(text: str | None) -> list[str]:
    words = [" ".join(w.split()).lower() for w in re.split(r"[,\n]", text or "")]
    return list(dict.fromkeys(w for w in words if w))[:MAX_KEYWORDS]


def keyword_hit(text: str, keywords: list[str], match: str) -> str | None:
    """The first keyword the message sets off, or None."""
    body = " ".join((text or "").lower().split())
    if not body:
        return None
    for k in keywords:
        if match == "exact":
            if body.strip(" .!?~") == k:
                return k
        elif match == "starts":
            if body.startswith(k):
                return k
        elif match == "contains":
            if k in body:
                return k
        elif re.search(rf"(?<![\w']){re.escape(k)}(?![\w'])", body):
            return k
    return None


# ------------------------------------------------------------ schedules
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_NAMES = {d: i for i, d in enumerate(DAYS)}
_DAY_NAMES.update({"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
                   "sunday": 6, "tues": 1, "weds": 2, "thur": 3, "thurs": 3})


@dataclass
class Schedule:
    every: timedelta | None = None           # "every 6h"
    days: list[int] = field(default_factory=list)  # weekdays (Monday 0); all seven for "daily"
    at: time | None = None

    def describe(self) -> str:
        if self.every:
            mins = int(self.every.total_seconds() // 60)
            return f"every {mins // 60} hour{'s' if mins // 60 != 1 else ''}" if mins % 60 == 0 else f"every {mins} minutes"
        when = self.at.strftime("%-I:%M %p") if self.at else ""
        if len(self.days) == 7:
            return f"daily at {when}"
        if self.days == [0, 1, 2, 3, 4]:
            return f"weekdays at {when}"
        if self.days == [5, 6]:
            return f"weekends at {when}"
        return ", ".join(DAYS[d].title() for d in self.days) + f" at {when}"


def parse_schedule(text: str) -> Schedule:
    """'every 30m', 'every 6h', 'daily 8pm', 'weekdays 9am', 'weekends noon', 'mon,fri 20:00', 'sunday 6pm'."""
    t = " ".join((text or "").lower().split())
    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)", t)
    if m:
        n = int(m[1])
        every = timedelta(minutes=n) if m[2].startswith("m") else timedelta(hours=n)
        if every < MIN_EVERY:
            raise ParseError("The shortest schedule is every 15 minutes.")
        if every > timedelta(days=7):
            raise ParseError("For less often than weekly, use a day and time, like 'sun 6pm'.")
        return Schedule(every=every)
    if t in ("hourly", "every hour"):
        return Schedule(every=timedelta(hours=1))
    m = re.fullmatch(r"([a-z, ]+?)\s+(?:at\s+)?(\S+(?:\s?[ap]m)?)", t)
    if not m:
        raise ParseError(f"I couldn't read \"{text}\" as a schedule. Try 'every 6h', 'daily 8pm' or 'mon,fri 20:00'.")
    day_part, at = m[1].strip(), parse_time(m[2])
    if day_part in ("daily", "every day", "everyday"):
        days = list(range(7))
    elif day_part == "weekdays":
        days = [0, 1, 2, 3, 4]
    elif day_part == "weekends":
        days = [5, 6]
    else:
        days = []
        for piece in re.split(r"[,\s]+", day_part.replace("every", "").strip()):
            key = piece.rstrip("s") if piece.rstrip("s") in _DAY_NAMES else piece
            if key not in _DAY_NAMES:
                raise ParseError(f"I don't know the day \"{piece}\". Try mon, tue, wed, thu, fri, sat or sun.")
            days.append(_DAY_NAMES[key])
        days = sorted(set(days))
    return Schedule(days=days, at=at)


def next_run(schedule: Schedule, after: datetime, tz: ZoneInfo) -> datetime:
    """The next time a schedule fires, strictly after `after` (an aware datetime)."""
    if schedule.every:
        return after + schedule.every
    local = after.astimezone(tz)
    for offset in range(0, 8):
        day: date = local.date() + timedelta(days=offset)
        if day.weekday() not in schedule.days:
            continue
        candidate = datetime.combine(day, schedule.at, tzinfo=tz)
        if candidate > local:
            return candidate.astimezone(after.tzinfo)
    raise ParseError("That schedule never fires.")


# ------------------------------------------------------------ replies
def split_replies(text: str) -> list[str]:
    """Replies to pick from, separated by a line with just --- (so a reply can have several lines)."""
    parts = re.split(r"(?m)^\s*-{3,}\s*$", text or "")
    return [p.strip() for p in parts if p.strip()][:MAX_REPLIES]


def fill(template: str, values: dict) -> str:
    """Put the placeholders in. Unknown {things} stay as written, so a stray brace can't break a reply."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m[1]]) if m[1] in values else m[0], template)


def pick(texts: list[str], rng: random.Random | None = None) -> str | None:
    return (rng or random).choice(texts) if texts else None


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n:,}{suffix}"


# ------------------------------------------------------------ which actions fit which trigger
def action_problem(trigger: str, action: dict) -> str | None:
    """Why an action can't work with a trigger, or None if it can."""
    kind = action.get("type")
    if kind in (REACT, PIN, REPOST) and trigger not in MESSAGE_TRIGGERS:
        return f"There's no message to {'react to' if kind == REACT else kind} when the trigger is \"{TRIGGERS[trigger]}\"."
    if kind == ROLE and trigger not in MEMBER_TRIGGERS:
        return "A schedule has no member to give a role to."
    if kind == COUNT and action.get("scope") == "member" and trigger not in MEMBER_TRIGGERS:
        return "A schedule has no member to count for; count server-wide instead."
    if kind == REPLY and not action.get("channel_id") and trigger not in MESSAGE_TRIGGERS:
        return "Say which channel to post in: there's no message to reply to."
    return None


def describe_trigger(a: Article, role_name=None) -> str:
    if a.trigger == KEYWORD:
        words = ", ".join(f"“{k}”" for k in split_keywords(a.value)) or "(no words yet)"
        return f"Says {words} ({a.match})"
    if a.trigger in (ROLE_ADDED, ROLE_REMOVED):
        role = role_name(int(a.value)) if role_name and a.value and a.value.isdigit() else f"<@&{a.value}>"
        return f"{'Gets' if a.trigger == ROLE_ADDED else 'Loses'} {role}"
    if a.trigger == REACTION:
        need = f"{a.threshold} " if a.threshold > 1 else ""
        return f"Gets {need}{a.value} reaction{'s' if a.threshold > 1 else ''}"
    if a.trigger == SCHEDULE:
        try:
            return f"Runs {parse_schedule(a.value or '').describe()}"
        except ParseError:
            return f"Runs on “{a.value}” (can't read it)"
    return TRIGGERS[a.trigger]


def describe_action(action: dict) -> str:
    kind = action.get("type")
    if kind == REPLY:
        n = len(action.get("texts") or [])
        where = f" in <#{action['channel_id']}>" if action.get("channel_id") else ""
        pic = " with a picture" if action.get("image") else ""
        first = (action.get("texts") or [""])[0].replace("\n", " ")
        preview = f": “{first[:60]}{'…' if len(first) > 60 else ''}”" if first else ""
        return f"{'Posts' if where else 'Replies'}{where}{pic}, one of {n}{preview}" if n > 1 else f"{'Posts' if where else 'Replies'}{where}{pic}{preview}"
    if kind == REACT:
        return f"Reacts {action.get('emoji')}"
    if kind == ROLE:
        verb = "Gives" if action.get("mode") == "add" else "Takes away"
        mins = action.get("minutes")
        return f"{verb} <@&{action.get('role_id')}>" + (f" for {mins} minutes" if mins else "")
    if kind == COUNT:
        return f"Counts {'per member' if action.get('scope') == 'member' else 'server-wide'}"
    if kind == REPOST:
        return f"Reposts it in <#{action.get('channel_id')}>"
    if kind == PIN:
        return "Pins it"
    return kind or "?"


def cooldown_key(article: Article, channel_id: int | None, member_id: int | None) -> tuple:
    if article.cooldown_scope == "member":
        return article.id, "m", member_id
    if article.cooldown_scope == "channel":
        return article.id, "c", channel_id
    return article.id, "s", None


def rolls(chance: int, rng: random.Random | None = None) -> bool:
    return chance >= 100 or (rng or random).random() * 100 < chance


_EMOJI = re.compile(r"<a?:\w+:\d+>|:([\w~]{2,32}):")


def server_emoji(text: str, emojis) -> str:
    """Turn :Bruh: into the server's own Bruh emoji. Pop-up forms can't use the emoji picker, so the short
    name is how the PDC type them. Unknown names and ready-made emoji are left alone."""
    if not text or not emojis:
        return text
    by_name = {}
    for e in emojis:
        by_name.setdefault(e.name, e)
        by_name.setdefault(e.name.lower(), e)

    def swap(m):
        if m.group(1) is None:
            return m.group(0)
        e = by_name.get(m.group(1)) or by_name.get(m.group(1).lower())
        return str(e) if e is not None else m.group(0)
    return _EMOJI.sub(swap, text)
