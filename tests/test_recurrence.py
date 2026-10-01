"""1.5.0: more repeat patterns, changing a repeat, ending a series on a date and skipping dates."""
from datetime import date, datetime, timedelta

import pytest

from ursula.voyage_logic import (ParseError, describe_repeat, following, next_occurrence, nth_of,
                                     parse_skips, repeat_choices, resolve_repeat, to_utc, upcoming_dates,
                                     valid_repeat)
from tests.test_voyages import PT, env, make_voyage, organizer  # noqa: F401


def at8(d: date) -> datetime:
    return to_utc(d, datetime.strptime("20:00", "%H:%M").time(), PT)


def local(dt: datetime) -> date:
    return dt.astimezone(PT).date()


# ------------------------------------------------------------ the patterns
def test_every_n_weeks_and_nth_weekday():
    sat = date(2026, 10, 10)                          # the 2nd Saturday of October
    assert nth_of(sat) == "nth:2:5" and nth_of(sat, last=True) == "nth:-1:5"
    assert nth_of(date(2026, 10, 31)) == "nth:-1:5"   # a 5th Saturday is taken as the last one
    assert local(next_occurrence(at8(sat), "weeks:3", PT)) == date(2026, 10, 31)
    assert local(next_occurrence(at8(sat), "nth:2:5", PT)) == date(2026, 11, 14)
    assert local(next_occurrence(at8(date(2026, 10, 30)), "nth:-1:4", PT)) == date(2026, 11, 27)
    dec = next_occurrence(at8(date(2026, 12, 12)), "nth:2:5", PT)
    assert local(dec) == date(2027, 1, 9) and dec.astimezone(PT).hour == 20
    assert describe_repeat("weeks:3") == "Every 3 weeks" and describe_repeat("weekly") == "Every week"
    assert describe_repeat("nth:-1:4") == "Every month on the last Friday"
    assert describe_repeat("nth:2:5") == "Every month on the 2nd Saturday"
    for good in ("none", "weekly", "biweekly", "monthly", "weeks:12", "nth:4:0"):
        assert valid_repeat(good)
    for bad in ("weeks:0", "weeks:13", "nth:5:1", "nth:1:7", "daily", "weeks:1; drop"):
        assert not valid_repeat(bad)


def test_choices_read_from_the_voyage_date():
    assert resolve_repeat("nth", date(2026, 10, 10)) == "nth:2:5"
    assert resolve_repeat("nth:last", date(2026, 10, 10)) == "nth:-1:5"
    assert resolve_repeat("Every 6 weeks", date(2026, 10, 10)) == "weeks:6"
    with pytest.raises(ParseError):
        resolve_repeat("every 40 weeks", date(2026, 10, 10))
    labels = [label for label, _ in repeat_choices(date(2026, 10, 10))]
    assert "Every month on the 10th" in labels and "Every month on the 2nd Saturday" in labels
    assert "Every month on the last Saturday" in labels
    # the last Saturday of the month only appears once when the date is already the last one
    assert [c for _, c in repeat_choices(date(2026, 10, 31))].count("nth:-1:5") == 1


def test_following_skips_dates_and_stops_at_the_end():
    start = at8(date(2026, 10, 2))
    skips = [date(2026, 10, 9), date(2026, 10, 16)]
    assert local(following(start, "weekly", PT, skips=skips)) == date(2026, 10, 23)
    assert following(start, "weekly", PT, until=date(2026, 10, 8)) is None
    assert following(start, "weekly", PT, skips=skips, until=date(2026, 10, 20)) is None
    assert local(following(start, "weekly", PT, after=at8(date(2026, 11, 1)))) == date(2026, 11, 6)
    assert following(start, "none", PT) is None
    dates = [local(d) for d in upcoming_dates(start, "weeks:2", PT, until=date(2026, 11, 13))]
    assert dates == [date(2026, 10, 16), date(2026, 10, 30), date(2026, 11, 13)]
    assert parse_skips("2026-10-09,junk,2026-10-09, 2026-10-02") == [date(2026, 10, 2), date(2026, 10, 9)]


# ------------------------------------------------------------ in Discord
async def only(bot):
    (v,) = await bot.db.voyages_with_status("scheduled")
    return v


async def test_create_with_a_pattern_and_an_end(env):
    bot, cog, guild, text = env
    inter = await make_voyage(cog, guild, repeat="nth", repeat_ends="2027-03-31")
    v = await only(bot)
    day = datetime.fromisoformat(v.starts_at).astimezone(await cog.tz(10)).date()
    assert v.repeat == nth_of(day) and v.repeat_until == "2027-03-31"
    bad = await make_voyage(cog, guild, repeat="weekly", repeat_ends="2020-01-01")
    assert "before" in bad.response.messages[0]


async def test_edit_repeat_skip_and_end_in_discord(env):
    bot, cog, guild, text = env
    await make_voyage(cog, guild, repeat="weekly")
    v = await only(bot)
    tz = await cog.tz(10)
    own = datetime.fromisoformat(v.starts_at).astimezone(tz).date()
    wk = [own + timedelta(days=7 * i) for i in range(1, 5)]

    me = organizer(guild)
    await cog.edit.callback(cog, me, voyage=str(v.id), skip=wk[0].isoformat(), repeat_ends=wk[2].isoformat())
    v = await bot.db.get_voyage(v.id)
    assert v.skips == wk[0].isoformat() and v.repeat_until == wk[2].isoformat()
    assert "~~" in me.followup.sent[-1] and "skipped" in me.followup.sent[-1]
    assert [(d, s) for d, s in await cog.series_dates(v)] == [(wk[0], True), (wk[1], False), (wk[2], False)]

    # a date that isn't in the series is refused
    nope = organizer(guild)
    await cog.edit.callback(cog, nope, voyage=str(v.id), skip=(own + timedelta(days=3)).isoformat())
    assert "isn't one of this series' dates" in nope.response.messages[0]
    # only the organizer
    stranger = organizer(guild, uid=2)
    await cog.edit.callback(cog, stranger, voyage=str(v.id), skip=wk[1].isoformat())
    assert "called it" in stranger.response.messages[0].lower()

    # skipping this voyage's own date cancels just it, and the next one goes up past the skipped week
    await cog.edit.callback(cog, organizer(guild), voyage=str(v.id), skip=own.isoformat())
    assert (await bot.db.get_voyage(v.id)).status == "cancelled"
    nxt = await only(bot)
    assert datetime.fromisoformat(nxt.starts_at).astimezone(tz).date() == wk[1]
    assert nxt.repeat_until == wk[2].isoformat() and nxt.skips == ""     # gone-by skips are dropped

    # after the end date, nothing more is posted
    await cog.edit.callback(cog, organizer(guild), voyage=str(nxt.id), skip=wk[1].isoformat())
    last = await only(bot)
    assert datetime.fromisoformat(last.starts_at).astimezone(tz).date() == wk[2]
    await cog.apply_cancel(guild, last.id)
    assert await bot.db.voyages_with_status("scheduled") == []


async def test_changing_the_repeat_starts_a_new_series_and_none_clears_it(env):
    bot, cog, guild, text = env
    await make_voyage(cog, guild, repeat="weekly", repeat_ends="2027-06-01")
    v = await only(bot)
    same = organizer(guild)                    # "Every week" picked again on a "weekly" voyage changes nothing
    await cog.edit.callback(cog, same, voyage=str(v.id), repeat="weeks:1")
    assert "anything to change" in same.response.messages[0] and (await bot.db.get_voyage(v.id)).repeat == "weekly"
    await cog.edit.callback(cog, organizer(guild), voyage=str(v.id), repeat="weeks:3")
    v = await bot.db.get_voyage(v.id)
    assert v.repeat == "weeks:3" and v.series_id == v.id and v.repeat_until == "2027-06-01"
    assert "Every 3 weeks" in cog.series_text(v, await cog.series_dates(v))
    await cog.edit.callback(cog, organizer(guild), voyage=str(v.id), repeat_ends="never")
    assert (await bot.db.get_voyage(v.id)).repeat_until is None
    await cog.edit.callback(cog, organizer(guild), voyage=str(v.id), repeat="none")
    v = await bot.db.get_voyage(v.id)
    assert v.repeat == "none" and v.repeat_until is None and v.skips == ""
    oops = organizer(guild)
    await cog.edit.callback(cog, oops, voyage=str(v.id), skip="2027-01-01")
    assert "doesn't repeat" in oops.response.messages[0]


async def test_series_command_and_autocomplete(env):
    bot, cog, guild, text = env
    await make_voyage(cog, guild, repeat="biweekly")
    v = await only(bot)
    me = organizer(guild)
    await cog.series.callback(cog, me, voyage=str(v.id))
    assert "Every 2 weeks" in me.response.messages[0] and "(this one)" in me.response.messages[0]
    me.namespace = type("NS", (), {"voyage": str(v.id), "date": None})()
    got = await cog.skip_ac(me, "")
    assert "this one" in got[0].name and len(got) == 25
    assert await cog.unskip_ac(me, "") == []
    reps = await cog.repeat_ac(me, "")
    assert any("Every month on the" in c.name for c in reps)
    assert [c.value for c in await cog.repeat_ac(me, "every 5 weeks")] == ["weeks:5"]


# ------------------------------------------------------------ from the screens
async def test_daisho_changes_the_series(env):
    from ursula.cogs.daisho import ApplyError, Daisho
    bot, cog, guild, text = env
    await make_voyage(cog, guild, repeat="weekly")
    v = await only(bot)
    tz = await cog.tz(10)
    own = datetime.fromisoformat(v.starts_at).astimezone(tz).date()
    d = Daisho(bot)
    msg = await d.apply_voyage_update(guild, {"id": v.id, "repeat": "nth:last", "repeat_until": "2027-09-01",
                                              "skip": [], "unskip": []})
    v = await bot.db.get_voyage(v.id)
    assert "updated" in msg and v.repeat == nth_of(own, last=True) and v.repeat_until == "2027-09-01"
    snap = (await d.snap_voyages(guild))[0]
    assert snap["repeat_label"].startswith("Every month on the last") and len(snap["series"]) == 10
    assert snap["repeat_choices"][0] == ["Doesn't repeat", "none"] and snap["local_date"] == own.isoformat()
    first = snap["series"][0]["date"]
    await d.apply_voyage_update(guild, {"id": v.id, "skip": [first]})
    assert (await bot.db.get_voyage(v.id)).skips == first
    await d.apply_voyage_update(guild, {"id": v.id, "unskip": first})
    assert (await bot.db.get_voyage(v.id)).skips == ""
    for bad in ({"skip": ["not a date"]}, {"skip": "2020-01-01"}, {"repeat": "daily"}, {"repeat": 7},
                {"repeat_until": "1999-01-01"}, {"repeat_until": "soon"}, {"skip": ["2026-01-01"] * 31}):
        with pytest.raises(ApplyError):
            await d.apply_voyage_update(guild, {"id": v.id, **bad})
    await d.apply_voyage_update(guild, {"id": v.id, "repeat_until": ""})
    assert (await bot.db.get_voyage(v.id)).repeat_until is None
    # skipping its own date from the screens cancels just this one
    msg = await d.apply_voyage_update(guild, {"id": v.id, "skip": own.isoformat()})
    assert "Skipped" in msg and (await bot.db.get_voyage(v.id)).status == "cancelled"
    assert len(await bot.db.voyages_with_status("scheduled")) == 1


async def test_daisho_creates_with_a_pattern(env):
    from ursula.cogs.daisho import ApplyError, Daisho
    bot, cog, guild, text = env
    cog.voyage_channel = lambda g, settings: text
    guild.get_member = lambda uid: guild.members.get(uid)
    guild.members[1].roles = []
    starts = (datetime.now(PT) + timedelta(days=5)).replace(hour=20, minute=0, second=0, microsecond=0)
    d = Daisho(bot)
    base = {"title": "Fort Night", "starts_at": starts.isoformat(), "member_id": 1}
    with pytest.raises(ApplyError):
        await d.apply_voyage_create(guild, {**base, "repeat": "every day"})
    with pytest.raises(ApplyError, match="before"):
        await d.apply_voyage_create(guild, {**base, "repeat": "weekly", "repeat_until": "2020-01-01"})
    await d.apply_voyage_create(guild, {**base, "repeat": "weeks:4", "repeat_until": "2027-12-31"})
    v = await only(bot)
    assert v.repeat == "weeks:4" and v.repeat_until == "2027-12-31"


# ------------------------------------------------------------ from the review
async def test_a_series_is_never_carried_on_twice(env):
    """While one voyage is under sail, re-planning or cancelling the next must not bring the old series back."""
    bot, cog, guild, text = env
    for what in ("repeat", "none", "cancel"):
        await make_voyage(cog, guild, repeat="weekly")
        a = await only(bot)
        await bot.db.update_voyage(a.id, status="started")        # A sails; B is posted
        b = await cog.schedule_next(guild, a)
        if what == "repeat":
            await cog.edit.callback(cog, organizer(guild), voyage=str(b.id), repeat="weeks:2")
        elif what == "none":
            await cog.edit.callback(cog, organizer(guild), voyage=str(b.id), repeat="none")
        else:
            await cog.apply_cancel(guild, b.id, whole_series=True)
        await cog.sweep(guild, await bot.db.get_voyage(a.id))     # the clock looks at A again
        waiting = await bot.db.voyages_with_status("scheduled")
        assert len(waiting) == (0 if what == "cancel" else 1), what
        for v in await bot.db.voyages_with_status("scheduled", "started"):
            await bot.db.update_voyage(v.id, status="ended")


async def test_moving_a_voyage_keeps_its_series_sensible(env):
    from ursula.cogs.daisho import ApplyError, Daisho
    bot, cog, guild, text = env
    await make_voyage(cog, guild, repeat="nth")
    v = await only(bot)
    tz = await cog.tz(10)
    old = datetime.fromisoformat(v.starts_at).astimezone(tz)
    d = Daisho(bot)
    new = (old + timedelta(days=3))
    await d.apply_voyage_update(guild, {"id": v.id, "starts_at": new.isoformat()})
    v = await bot.db.get_voyage(v.id)
    assert v.repeat == nth_of(new.date()) and v.series_id == v.id
    await d.apply_voyage_update(guild, {"id": v.id, "repeat_until": (new.date() + timedelta(days=40)).isoformat()})
    with pytest.raises(ApplyError, match="last date"):
        await d.apply_voyage_update(guild, {"id": v.id, "starts_at": (new + timedelta(days=60)).isoformat()})
    for bad in (20291010, "20291010", ["2029-10-10"]):
        with pytest.raises(ApplyError, match="end date"):
            await d.apply_voyage_update(guild, {"id": v.id, "repeat_until": bad})


def test_a_daylight_saving_gap_doesnt_shift_the_series():
    from datetime import time
    gap = to_utc(date(2027, 3, 14), time(2, 30), PT)          # 2:30 doesn't exist that night: it's 3:30
    nxt = next_occurrence(gap, "weekly", PT, at=time(2, 30))
    assert nxt.astimezone(PT).hour == 2 and nxt.astimezone(PT).minute == 30
