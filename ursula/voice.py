"""Ursula's voice: a dry, warm Odonian who'd rather everyone just helped each other.

Ursula is Anarres's bot, named for Ursula K. Le Guin; the server and its places borrow from The
Dispossessed (Abbenay, the PDC, syndicates, the Ansible) and her other books. Every member-facing line
lives here so the voice stays consistent and is easy to tune. Admin replies and logs stay in plain English
on purpose; they live in the cogs.

Lines are templates for str.format(). Keep them original: allusions and names are fine, but no quotes
from the books (or songs, or anyone else's writing).
"""
from __future__ import annotations

import random

CUSSES = [
    "Dust and dry wind!",
    "Odo's spectacles!",
    "Egoizing gears!",
    "Well, that's propertarian of it.",
    "Holum fibre and patience!",
    "By the Abbenay sky!",
    "Oh, for a working ansible.",
    "Rocks, sand and paperwork!",
]

# Unwarranted Leftist Aphorisms (the U.R.S.U.L.A. backronym's last two words): original, small, sincere.
APHORISMS = [
    "Nobody owns the music. Everybody brings the snacks.",
    "Mutual aid is just good manners with a long memory.",
    "A wall is only ever half a wall: the other half is what it keeps you from.",
    "Share the load and it stops being a load.",
    "Plans are better when the people they're for made them.",
    "Rest is not a reward. It's a right.",
    "The best rule is the one nobody had to enforce.",
    "There's always room for one more at the table if everybody scoots.",
]

LINES: dict[str, list[str]] = {
    "about": [
        "Ursula {version}: Unconventional Replies, Schedules & Unwarranted Leftist Aphorisms. I keep the "
        "calendar, the customs and the music for Anarres, and I don't own any of it. {aphorism}",
        "Ursula here, version {version}. I coordinate; I don't command. Ask me about gatherings, syndicates "
        "or what's playing. {aphorism}",
    ],
    "error": [
        "{cuss} Something seized up in my works. It's been noted for the PDC. Try again in a moment?",
    ],
    "guild_only": [
        "{cuss} That only works on the server itself, not in private messages.",
    ],
    "no_permission": [
        "{cuss} That one's for the PDC (the server's admins). Nothing personal; it's a posting, not a rank.",
    ],
    "image_bad": [
        "{cuss} I can't put that on the card. A PNG, JPG, GIF or WEBP up to 10 MB, please.",
    ],
    # ------------------------------------------------------------ time zones
    "tz_saved": [
        "Noted: I'll read the times you type as {zone}. It's {local} there now. Everyone else still sees "
        "times in their own zone.",
    ],
    "tz_unknown": [
        "{cuss} I don't know the time zone \"{zone}\". Try Pacific, ET, America/Chicago or Europe/London.",
    ],
    "tz_mine": [
        "I read the times you type as {zone}. It's {local} there now.",
    ],
    "tz_mine_region": [
        "Your region role says {zone}, so that's how I read the times you type. It's {local} there now. "
        "Not quite right? `/timezone set` picks your own.",
    ],
    "tz_none": [
        "You haven't set a time zone, so I read the times you type as the server's: {zone}. Set yours with "
        "`/timezone set`.",
    ],
    "tz_cleared": [
        "Forgotten. I'll read your times in the server's time zone again.",
    ],
    "tz_cleared_region": [
        "Forgotten. I'll go by your region role again: {zone}.",
    ],
    # ------------------------------------------------------------ syndicates (role menus)
    "colours_prompt": [
        "Which {title} are yours? Pick below and I'll sort the rest.",
    ],
    "colours_done": [
        "Done. {changes}",
        "Your syndicates are updated. {changes}",
    ],
    "colours_same": [
        "Nothing to change: that's exactly what you already have.",
    ],
    "colours_cant": [
        "{cuss} I couldn't change your roles. Someone in the PDC needs to move my role above these ones.",
    ],
    "colours_gone": [
        "{cuss} That syndicate's card has been taken down. The PDC can post a fresh one.",
    ],
    "colours_zone_prompt": [
        "Your region covers a lot of ground. Which of these is closest? It's the zone I'll read your typed "
        "times in.",
    ],
    # ------------------------------------------------------------ gatherings (internally: voyages)
    "crew_cant_post": [
        "{cuss} I can't post a card here. The PDC needs to give me Send Messages and Embed Links in this "
        "channel.",
    ],
    "crew_no_threads": [
        "{cuss} Gatherings need a regular text channel, not a thread. Try the main channel.",
    ],
    "crew_ping": [
        "{role}, there's a gathering you might like.",
    ],
    "voyage_posted": [
        "{organizer} is calling a gathering: **{title}**. Say if you're coming.",
        "A gathering on the board: **{title}**, called by {organizer}. All welcome.",
    ],
    "voyage_created": [
        "Your gathering is on the board for {when}. Reminders go out {reminders}. {link}",
    ],
    "voyage_aboard": [
        "You're going to **{title}**. I'll remind you beforehand.",
    ],
    "voyage_waitlist": [
        "{cuss} **{title}** is full, so you're on the waitlist. I'll tell you the moment a place opens.",
    ],
    "voyage_maybe": [
        "Down as a maybe for **{title}**. I'll keep you posted.",
    ],
    "voyage_cant": [
        "Marked as can't make it for **{title}**. There'll be others.",
    ],
    "voyage_removed": [
        "Answer cleared. Pick again any time.",
    ],
    "voyage_promoted": [
        "Good news, {names}: a place opened at **{title}** and you're going.",
    ],
    "voyage_reminder": [
        "{names}: **{title}** is {when}. {link}",
        "A nudge, {names}: **{title}** starts {when}. {link}",
    ],
    "voyage_starting": [
        "{names}, **{title}** is starting now. {place}",
        "It's time, {names}: **{title}** is under way. {place}",
    ],
    "voyage_cancelled": [
        "{cuss} **{title}** has been called off. Sorry, {names}.",
    ],
    "voyage_cancel_done": [
        "Called off, and everyone who'd answered has been told.",
    ],
    "voyage_edited": [
        "Gathering updated, and its Discord Event with it. It's now {when}. {link}",
    ],
    "voyage_nothing_changed": [
        "You didn't give me anything to change. Pick at least one thing.",
    ],
    "voyage_bad_input": [
        "{cuss} {error}",
    ],
    "voyage_bad_time": [
        "{cuss} That time is in the past or more than a year away. Pick one in the future.",
    ],
    "voyage_not_yours": [
        "{cuss} Only whoever called it (or the PDC) can change that gathering.",
    ],
    "voyage_none": [
        "{cuss} I can't find that gathering. It may be over or called off.",
    ],
    "voyage_over": [
        "{cuss} That gathering is already under way, over, or called off.",
    ],
    "voyage_none_upcoming": [
        "Nothing on the board yet. Call one with `/gathering call`.",
    ],
    "voyage_list_header": [
        "Gatherings on the board:",
    ],
    # ------------------------------------------------------------ Salas (music)
    "music_need_voice": [
        "Hop into a voice channel first, and I'll bring the music to you.",
    ],
    "music_cant_join": [
        "{cuss} I can't join or speak in that voice channel. The PDC needs to let me in.",
    ],
    "music_elsewhere": [
        "{cuss} I'm already playing in {channel}. Join in there, or wait till that set's done.",
    ],
    "music_dj_only": [
        "{cuss} Only the DJs (or whoever asked for this track) can do that.",
    ],
    "music_same_channel": [
        "You'll need to be in {channel} with me to steer the music.",
    ],
    "music_nothing": [
        "Nothing's playing. Start something with `/play`.",
    ],
    "music_off": [
        "Salas (music) is switched off on this server.",
    ],
    "music_now": [
        "Now playing **{title}**.",
    ],
    "music_queued": [
        "**{title}** is in the queue at number {position}.",
    ],
    "music_queued_many": [
        "Loaded {count} tracks from **{name}** into the queue.",
    ],
    "music_paused": [
        "Paused. `/salas resume` (or the button) picks it back up.",
    ],
    "music_resumed": [
        "And we're back.",
    ],
    "music_skipped": [
        "Skipped **{title}**.",
    ],
    "music_stopped": [
        "Music stopped and the queue cleared. Everybody gets their ears back.",
    ],
    "music_left_idle": [
        "Nobody's listening, so I've packed up. `/play` brings me back.",
    ],
    "music_queue_end": [
        "That's the end of the queue. Add more with `/play`.",
    ],
    "music_failed_track": [
        "{cuss} I couldn't play **{title}** ({reason}), so I've skipped it.",
    ],
    "music_your_share": [
        "You've already got {count} songs waiting. Let the others' play too; it's a shared queue.",
    ],
    "music_queue_full": [
        "{cuss} The queue's full. Let a few play first.",
    ],
}

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]


def cuss(rng: random.Random | None = None) -> str:
    return (rng or random).choice(CUSSES)


def aphorism(rng: random.Random | None = None) -> str:
    return (rng or random).choice(APHORISMS)


def say(key: str, rng: random.Random | None = None, **values) -> str:
    """Pick a line for `key` and fill it in. `{cuss}` and `{aphorism}` are always available."""
    r = rng or random
    template = r.choice(LINES[key])
    values.setdefault("cuss", cuss(r))
    values.setdefault("aphorism", aphorism(r))
    return template.format(**values)


def format_date(month: int, day: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {day}"


def join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f" and {names[-1]}"
