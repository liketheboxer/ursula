"""Region roles set members' time zones, but never over one they chose themselves."""
from types import SimpleNamespace

import pytest

from ursula.region_logic import MANUAL, REGION, decide, guess_zone, region_zone_for

# The Fortress's region roles, as named on the server
FORTRESS = {
    "North America - West": "America/Los_Angeles",
    "North America - Mountain & Central": "America/Chicago",
    "North America - East": "America/New_York",
    "UK": "Europe/London",
    "Australia": "Australia/Sydney",
    "EU-West": "Europe/Berlin",
    "EU-East": "Europe/Athens",
    "Asia": None,
    "South/Central America": None,
}


@pytest.mark.parametrize("name,zone", FORTRESS.items())
def test_fortress_region_roles(name, zone):
    g = guess_zone(name)
    assert g is not None
    assert g.zone == zone


def test_split_and_other_region_names():
    assert guess_zone("North America - Mountain").zone == "America/Denver"
    assert guess_zone("North America - Central").zone == "America/Chicago"
    assert guess_zone("NA Pacific").zone == "America/Los_Angeles"
    assert guess_zone("Australia - West").zone == "Australia/Perth"
    assert guess_zone("New Zealand").zone == "Pacific/Auckland"
    assert guess_zone("Europe (Central)").zone == "Europe/Berlin"
    assert guess_zone("North America - Mountain & Central").note  # flagged as rough


@pytest.mark.parametrize("name", ["Sea of Thieves", "Among Us", "Quartermaster", "Deep Rock Galactic",
                                  "Lethal Company", "Helldivers 2", "West Marches", "Void Crew", "@everyone"])
def test_non_region_roles_are_ignored(name):
    assert guess_zone(name) is None


def test_region_zone_for_last_listed_wins():
    mapping = {1: "America/New_York", 2: "Europe/London"}
    assert region_zone_for([9, 1], mapping) == "America/New_York"
    assert region_zone_for([1, 9, 2], mapping) == "Europe/London"
    assert region_zone_for([9], mapping) is None


def test_decide():
    ny, ldn = "America/New_York", "Europe/London"
    assert decide(None, ny) == ("set", ny)
    assert decide((ny, REGION), ny) is None
    assert decide((ny, REGION), ldn) == ("set", ldn)
    assert decide((ny, REGION), None) == ("clear", None)
    assert decide(None, None) is None
    assert decide((ldn, MANUAL), ny) is None      # their own choice wins
    assert decide((ldn, MANUAL), None) is None


# ------------------------------------------------------------ the cog, with fake members
class Member(SimpleNamespace):
    pass


def member(uid, guild, *role_ids, bot=False):
    return Member(id=uid, bot=bot, guild=guild, roles=[SimpleNamespace(id=r) for r in role_ids])


@pytest.fixture
async def env(tmp_path):
    from tests.test_bot import make_config
    from ursula.bot import Ursula
    bot = Ursula(make_config(tmp_path))
    await bot.db.connect()
    await bot.load_extension("ursula.cogs.regions")
    await bot.load_extension("ursula.cogs.core")
    guild = SimpleNamespace(id=5, members=[])
    await bot.db.set_region_zone(5, 101, "America/New_York")
    await bot.db.set_region_zone(5, 102, "Europe/London")
    yield bot, bot.get_cog("Regions"), guild
    await bot.db.close()


async def test_role_change_sets_and_clears(env):
    bot, cog, guild = env
    before = member(1, guild)
    after = member(1, guild, 101)
    await cog.on_member_update(before, after)
    assert await bot.db.member_timezone_source(1) == ("America/New_York", REGION)
    await cog.on_member_update(after, member(1, guild, 102))
    assert await bot.db.member_timezone(1) == "Europe/London"
    await cog.on_member_update(member(1, guild, 102), member(1, guild))
    assert await bot.db.member_timezone(1) is None


async def test_manual_choice_is_never_overwritten(env):
    bot, cog, guild = env
    await bot.db.set_member_timezone(2, "America/Denver")  # /timezone set
    await cog.on_member_update(member(2, guild), member(2, guild, 101))
    assert await bot.db.member_timezone_source(2) == ("America/Denver", MANUAL)
    await cog.on_member_update(member(2, guild, 101), member(2, guild))
    assert await bot.db.member_timezone(2) == "America/Denver"


async def test_sync_backfills_existing_members(env):
    bot, cog, guild = env
    await bot.db.set_member_timezone(3, "Asia/Tokyo")
    guild.members = [member(1, guild, 101), member(2, guild, 102), member(3, guild, 101),
                     member(4, guild), member(9, guild, 101, bot=True)]
    r = await cog.sync_guild(guild)
    assert (r.set, r.cleared, r.kept_manual) == (2, 0, 1)
    assert await bot.db.member_timezone(1) == "America/New_York"
    assert await bot.db.member_timezone(2) == "Europe/London"
    assert await bot.db.member_timezone(3) == "Asia/Tokyo"
    assert await bot.db.member_timezone(9) is None
    again = await cog.sync_guild(guild)
    assert (again.set, again.cleared) == (0, 0)


async def test_no_mapping_does_nothing(env):
    bot, cog, _ = env
    other = SimpleNamespace(id=6, members=[])
    await cog.on_member_update(member(1, other), member(1, other, 101))
    assert await bot.db.member_timezone(1) is None


async def test_existing_zones_count_as_manual(tmp_path):
    from ursula.db import Database
    d = Database(tmp_path / "x.db")
    await d.connect()
    await d.set_member_timezone(1, "America/Chicago")
    assert await d.member_timezone_source(1) == ("America/Chicago", MANUAL)
    await d.set_region_zone(5, 101, "UTC")
    await d.set_region_zone(5, 101, None)
    assert await d.region_zones(5) == {}
    await d.close()


RECOMMENDED = {
    "North America - Hawaii": "Pacific/Honolulu",
    "North America - Alaska": "America/Anchorage",
    "North America - Pacific": "America/Los_Angeles",
    "North America - Arizona": "America/Phoenix",
    "North America - Mountain": "America/Denver",
    "North America - Central": "America/Chicago",
    "North America - Eastern": "America/New_York",
    "North America - Atlantic": "America/Halifax",
    "North America - Newfoundland": "America/St_Johns",
    "Australia - Western": "Australia/Perth",
    "Australia - Northern Territory": "Australia/Darwin",
    "Australia - South Australia": "Australia/Adelaide",
    "Australia - Queensland": "Australia/Brisbane",
    "Australia - Eastern": "Australia/Sydney",
}


@pytest.mark.parametrize("name,zone", RECOMMENDED.items())
def test_recommended_role_names(name, zone):
    g = guess_zone(name)
    assert g.zone == zone and not g.note
