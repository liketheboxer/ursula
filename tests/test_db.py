import pytest

from ursula.db import MIGRATIONS, Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.connect()
    yield d
    await d.close()


async def test_migrations_are_recorded_and_rerun_safely(tmp_path):
    path = tmp_path / "again.db"
    first = Database(path)
    await first.connect()
    await first.set_birthday(1, 2, 3, 4)
    await first.close()
    second = Database(path)
    await second.connect()  # must not re-run migration 1 or lose data
    assert await second.migrate() == len(MIGRATIONS)
    assert await second.get_birthday(1, 2) == (3, 4)
    await second.close()


async def test_birthday_set_update_remove(db):
    assert await db.get_birthday(10, 20) is None
    await db.set_birthday(10, 20, 5, 17)
    await db.set_birthday(10, 20, 6, 1)
    assert await db.get_birthday(10, 20) == (6, 1)
    assert await db.birthdays(10) == [(20, 6, 1)]
    assert await db.birthdays(99) == []
    assert await db.remove_birthday(10, 20)
    assert not await db.remove_birthday(10, 20)
    assert await db.count_birthdays() == 0


async def test_settings_defaults_and_updates(db):
    s = await db.get_settings(7)
    assert s.birthday_hour == 9 and s.birthday_channel_id is None
    s = await db.update_settings(7, birthday_channel_id=123, birthday_hour=18, timezone="UTC")
    assert (s.birthday_channel_id, s.birthday_hour, s.timezone) == (123, 18, "UTC")
    s = await db.update_settings(7, birthday_role_id=None)
    assert s.birthday_channel_id == 123
    assert [x.guild_id for x in await db.all_settings()] == [7]
    with pytest.raises(ValueError):
        await db.update_settings(7, nope=8)


async def test_role_grants_expire_by_day(db):
    await db.record_role_grant(1, 50, 900, "2026-09-28")
    await db.record_role_grant(1, 51, 900, "2026-09-29")
    assert await db.stale_role_grants(1, "2026-09-29") == [(50, 900)]
    await db.clear_role_grant(1, 50)
    assert await db.stale_role_grants(1, "2026-09-29") == []


async def test_role_grants_from_a_later_local_date_are_kept(db):
    await db.record_role_grant(1, 60, 900, "2026-09-30")
    assert await db.stale_role_grants(1, "2026-09-29") == []


async def test_birthday_change_history_survives_removal(db):
    await db.set_birthday(1, 2, 3, 4)
    await db.record_birthday_change(1, 2, "2026-09-01T12:00:00+00:00")
    await db.remove_birthday(1, 2)
    assert await db.last_birthday_change(1, 2) == "2026-09-01T12:00:00+00:00"
