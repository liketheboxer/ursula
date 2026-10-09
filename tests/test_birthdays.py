"""Birthdays (1.5.0, from PlunderBot): the date rules, members' own commands, and the day itself."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

from tests.test_bot import bot  # noqa: F401
from ursula.birthday_logic import (change_allowed, due_to_announce, is_birthday_on, next_occurrence, upcoming,
                                   valid_date)

UTC = timezone.utc


# ------------------------------------------------------------ the date rules
def test_valid_dates():
    assert valid_date(2, 29) and valid_date(12, 31)
    assert not valid_date(2, 30) and not valid_date(4, 31) and not valid_date(13, 1) and not valid_date(1, 0)


def test_leap_day_birthdays_move_to_feb_28_in_common_years():
    assert is_birthday_on(2, 29, date(2027, 2, 28))
    assert not is_birthday_on(2, 29, date(2028, 2, 28))
    assert is_birthday_on(2, 29, date(2028, 2, 29))
    assert is_birthday_on(7, 4, date(2026, 7, 4)) and not is_birthday_on(7, 4, date(2026, 7, 5))


def test_next_occurrence_and_upcoming():
    today = date(2026, 12, 30)
    assert next_occurrence(12, 30, today) == date(2026, 12, 30)
    assert next_occurrence(1, 2, today) == date(2027, 1, 2)
    assert next_occurrence(2, 29, date(2026, 3, 1)) == date(2027, 2, 28)
    assert next_occurrence(2, 29, date(2027, 3, 1)) == date(2028, 2, 29)
    result = upcoming([(1, 1, 5), (2, 9, 29), (3, 10, 1), (4, 9, 28)], date(2026, 9, 29), limit=3)
    assert [uid for uid, _ in result] == [2, 3, 1]


def test_due_to_announce_at_or_after_the_hour_once_per_day():
    tz = ZoneInfo("America/Los_Angeles")
    assert not due_to_announce(datetime(2026, 9, 29, 8, 59, tzinfo=tz), 9, None)
    assert due_to_announce(datetime(2026, 9, 29, 9, 0, tzinfo=tz), 9, None)
    assert due_to_announce(datetime(2026, 9, 29, 22, 0, tzinfo=tz), 9, "2026-09-28")
    assert not due_to_announce(datetime(2026, 9, 29, 22, 0, tzinfo=tz), 9, "2026-09-29")
    # moving the server's zone west never repeats a day
    assert not due_to_announce(datetime(2026, 9, 29, 20, 0, tzinfo=tz), 0, "2026-09-30")


def test_the_change_limit():
    t0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert change_allowed(None, t0) == (True, True, None)
    assert change_allowed(t0, t0 + timedelta(minutes=30)) == (True, False, None)       # a typo fix
    allowed, _, until = change_allowed(t0, t0 + timedelta(days=2))
    assert not allowed and until == t0 + timedelta(days=30)
    assert change_allowed(t0, t0 + timedelta(days=30)) == (True, True, None)


# ------------------------------------------------------------ a fake server
class Member:
    def __init__(self, uid, name, guild):
        self.id, self.display_name, self.guild = uid, name, guild
        self.mention, self.roles = f"<@{uid}>", []

    async def add_roles(self, role, reason=None):
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)


class Channel:
    def __init__(self, cid):
        self.id, self.sent = cid, []

    async def send(self, content=None, allowed_mentions=None):
        self.sent.append((content, allowed_mentions))


class Guild:
    def __init__(self):
        self.id = 10
        self.channel = Channel(20)
        self.role = SimpleNamespace(id=30, name="Birthday")
        self.members = {uid: Member(uid, name, self) for uid, name in ((1, "Takver"), (2, "Shevek"), (3, "Bedap"))}

    def get_member(self, uid):
        return self.members.get(uid)

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    def get_role(self, rid):
        return self.role if rid == self.role.id else None


# ------------------------------------------------------------ members' own commands
async def test_members_save_change_and_remove_their_own(bot):
    cog = bot.get_cog("Birthdays")
    assert "March 3" in await cog.set_as(10, 1, 3, 3)
    assert await bot.db.get_birthday(10, 1) == (3, 3)
    assert "March 4" in await cog.set_as(10, 1, 3, 4)                   # within the hour: a typo fix
    assert "isn't on any calendar" in await cog.set_as(10, 1, 2, 30)
    assert await bot.db.get_birthday(10, 1) == (3, 4)
    # a month later the window has closed; a week after a change, it's locked
    await bot.db.record_birthday_change(10, 1, (datetime.now(UTC) - timedelta(days=7)).isoformat())
    assert "stays put until" in await cog.set_as(10, 1, 6, 1)
    assert "I have you down for March 4" in await cog.mine_as(10, 1)
    assert "Forgotten" in await cog.remove_as(10, 1)
    assert "don't have a birthday" in await cog.remove_as(10, 1)
    assert await bot.db.get_birthday(10, 1) is None


async def test_upcoming_lists_only_people_still_here_and_pings_nobody(bot):
    cog, guild = bot.get_cog("Birthdays"), Guild()
    assert "Nobody's saved" in await cog.upcoming_text(guild)
    await bot.db.set_birthday(10, 1, 10, 9)
    await bot.db.set_birthday(10, 2, 1, 2)
    await bot.db.set_birthday(10, 99, 10, 10)                           # left the server
    text = await cog.upcoming_text(guild, now_utc=datetime(2026, 10, 9, 18, 0, tzinfo=UTC))
    assert text.splitlines()[1:] == ["- today!: <@1>", "- January 2: <@2>"]


async def test_leaving_the_server_forgets_the_date(bot):
    cog, guild = bot.get_cog("Birthdays"), Guild()
    await bot.db.set_birthday(10, 3, 5, 5)
    await cog.on_member_remove(guild.members[3])
    assert await bot.db.get_birthday(10, 3) is None


# ------------------------------------------------------------ the day itself
async def test_the_day_is_announced_once_with_the_role_until_tomorrow(bot):
    cog, guild = bot.get_cog("Birthdays"), Guild()
    await bot.db.set_birthday(10, 1, 10, 9)
    await bot.db.set_birthday(10, 2, 10, 9)
    await bot.db.set_birthday(10, 3, 10, 10)
    s = await bot.db.update_settings(10, timezone="America/Los_Angeles", birthday_hour=9, birthday_role_id=30)
    morning = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)                 # 8 AM in Los Angeles
    await cog.run_for_guild(guild, s, now_utc=morning)
    assert guild.channel.sent == []                                     # no channel set: nothing announced
    s = await bot.db.update_settings(10, birthday_channel_id=20)
    await cog.run_for_guild(guild, s, now_utc=morning)
    assert guild.channel.sent == []                                     # not 9 yet
    nine = morning + timedelta(hours=1)
    await cog.run_for_guild(guild, s, now_utc=nine)
    (text, mentions), = guild.channel.sent
    assert "<@2> and <@1>" in text                                     # Shevek, Takver: sorted by name
    assert {m.id for m in mentions.users} == {1, 2} and not mentions.everyone and not mentions.roles
    assert guild.role in guild.members[1].roles and guild.role not in guild.members[3].roles
    await cog.run_for_guild(guild, await bot.db.get_settings(10), now_utc=nine + timedelta(hours=3))
    assert len(guild.channel.sent) == 1                                 # once a day
    # the next day: the role comes off, and Bedap's turn
    await cog.run_for_guild(guild, await bot.db.get_settings(10), now_utc=nine + timedelta(days=1))
    assert guild.role not in guild.members[1].roles and guild.role in guild.members[3].roles
    assert "<@3>" in guild.channel.sent[-1][0]


async def test_a_missing_channel_never_repeats_the_day(bot):
    cog, guild = bot.get_cog("Birthdays"), Guild()
    await bot.db.set_birthday(10, 1, 10, 9)
    s = await bot.db.update_settings(10, timezone="UTC", birthday_hour=0, birthday_channel_id=404)
    await cog.run_for_guild(guild, s, now_utc=datetime(2026, 10, 9, 1, 0, tzinfo=UTC))
    assert (await bot.db.get_settings(10)).birthday_last_announced == "2026-10-09"


async def test_the_pdc_and_daisho_settings(bot):
    from ursula.cogs.daisho import SETTINGS
    assert SETTINGS["birthday_hour"] == ("int", 0, 23) and SETTINGS["birthday_channel_id"][0] == "text"
    admin = bot.get_cog("Admin")
    s = await bot.db.update_settings(10, birthday_channel_id=20, birthday_hour=18, birthday_role_id=30)
    await bot.db.set_birthday(10, 1, 1, 1)
    assert await admin.birthdays_line(10, s) == ("<#20> at 6:00 PM America/Los_Angeles · <@&30> for the day "
                                                 "· 1 on file")
    s = await bot.db.update_settings(10, birthday_channel_id=None)
    assert (await admin.birthdays_line(10, s)).startswith("not announced")
    assert discord.utils.get(bot.tree.get_commands(), name="birthday") is not None
