import random
import string

import pytest

from ursula import voice


@pytest.mark.parametrize("key", sorted(voice.LINES))
def test_every_line_fills_in(key):
    # Every placeholder any line might use, so a typo'd field fails here, not in Discord.
    values = dict(version="1.2.3", names="@Takver", role="@Film Club", channel="#voice", organizer="@Boxer",
                  title="Film Night", link="https://x", reminders="1 day, 1 hour before", error="Bad date.",
                  when="in 1 hour", zone="America/New_York (EDT)", local="8:00 PM", member="@Newbie",
                  changes="Added @UK.", forum="#threads", position=3, count=12, name="Work Songs",
                  reason="it's private", place="Where: Abbenay Square")
    fields = {f for line in voice.LINES[key] for _, f, _, _ in string.Formatter().parse(line) if f}
    assert fields <= set(values) | {"cuss", "aphorism"}, f"{key} uses unknown fields: {fields - set(values)}"
    for i in range(len(voice.LINES[key]) * 4):
        text = voice.say(key, rng=random.Random(i), **values)
        assert text and "{" not in text
        assert len(text) <= 2000  # Discord's message limit


def test_join_names():
    assert voice.join_names(["A"]) == "A"
    assert voice.join_names(["A", "B"]) == "A and B"
    assert voice.join_names(["A", "B", "C"]) == "A, B and C"


def test_no_real_swears():
    banned = {"fuck", "shit", "damn", "bitch", "ass "}
    everything = " ".join(voice.CUSSES + [line for lines in voice.LINES.values() for line in lines]).lower()
    assert not any(word in everything for word in banned)


def test_no_pirates_left():
    everything = " ".join(voice.CUSSES + voice.APHORISMS + [l for lines in voice.LINES.values() for l in lines]).lower()
    for word in ("pirate", "arr", "ahoy", "voyage", "crew", "ship", "captain", "plunderbot", "fortress"):
        assert f" {word}" not in everything, word
