"""Region roles and time zones: which zone a region role stands for.

A region role (e.g. "North America - East") picked from the role menu also sets the member's
time zone, unless they chose one themselves with /timezone set. Roles are matched to zones by
name once (/pdc regions auto) and stored, so the PDC can override any of them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REGION = "region"   # a zone that came from a region role
MANUAL = "manual"   # a zone the member chose with /timezone set


def normalise(name: str) -> str:
    """Lowercase words only: "EU-West" -> "eu west", "Mountain & Central" -> "mountain and central"."""
    name = name.lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", name))


def _has(words: str, *phrases: str) -> bool:
    padded = f" {words} "
    return any(f" {p} " in padded for p in phrases)


@dataclass(frozen=True)
class Guess:
    zone: str | None
    note: str = ""  # why a guess is rough, shown to the PDC


def guess_zone(role_name: str) -> Guess | None:
    """Best guess at the zone a region role stands for. None means it isn't a region role we know.
    A Guess with zone None is a region too broad for one zone (members set theirs themselves)."""
    w = normalise(role_name)
    north_america = _has(w, "north america", "na", "usa", "us", "canada")
    if _has(w, "south america", "latin america", "latam", "south and central america",
            "central and south america", "south central america"):
        return Guess(None, "spans several zones")
    if _has(w, "mountain") and _has(w, "central"):
        return Guess("America/Chicago", "covers Mountain and Central; Mountain members are an hour off")
    if _has(w, "arizona"):
        return Guess("America/Phoenix")  # no daylight saving
    if _has(w, "newfoundland"):
        return Guess("America/St_Johns")
    if _has(w, "atlantic", "ast", "adt"):
        return Guess("America/Halifax")
    if _has(w, "mountain", "mst", "mdt"):
        return Guess("America/Denver")
    if _has(w, "central", "cst", "cdt") and north_america or _has(w, "cst", "cdt"):
        return Guess("America/Chicago")
    if _has(w, "pacific", "pst", "pdt") or north_america and _has(w, "west", "western"):
        return Guess("America/Los_Angeles")
    if _has(w, "est", "edt") or north_america and _has(w, "east", "eastern"):
        return Guess("America/New_York")
    if _has(w, "alaska"):
        return Guess("America/Anchorage")
    if _has(w, "hawaii"):
        return Guess("Pacific/Honolulu")
    if _has(w, "uk", "united kingdom", "britain", "great britain", "ireland", "gmt"):
        return Guess("Europe/London")
    if _has(w, "eu", "europe") and _has(w, "west", "western", "central", "cet"):
        return Guess("Europe/Berlin")
    if _has(w, "eu", "europe") and _has(w, "east", "eastern", "eet"):
        return Guess("Europe/Athens")
    if _has(w, "new zealand", "nz"):
        return Guess("Pacific/Auckland")
    if _has(w, "australia", "aus"):
        if _has(w, "west", "western", "perth"):
            return Guess("Australia/Perth")
        if _has(w, "northern territory", "nt", "darwin"):
            return Guess("Australia/Darwin")  # no daylight saving
        if _has(w, "central", "south australia", "adelaide"):
            return Guess("Australia/Adelaide")
        if _has(w, "queensland", "qld", "brisbane"):
            return Guess("Australia/Brisbane")  # no daylight saving
        if _has(w, "east", "eastern", "sydney", "melbourne"):
            return Guess("Australia/Sydney")
        return Guess("Australia/Sydney", "east coast; Perth, Darwin, Adelaide and Brisbane members differ")
    if _has(w, "asia", "apac"):
        return Guess(None, "spans several zones")
    return None


def region_zone_for(role_ids: list[int], mapping: dict[int, str]) -> str | None:
    """The zone for a member holding these roles (in the order Discord lists them).
    With more than one mapped region role, the last one listed wins."""
    zones = [mapping[r] for r in role_ids if r in mapping]
    return zones[-1] if zones else None


def decide(current: tuple[str, str] | None, region_zone: str | None) -> tuple[str, str | None] | None:
    """What to do with a member's saved zone after their region roles change.
    current is (zone, source) or None. Returns ("set", zone), ("clear", None), or None for no change.
    A zone the member chose themselves is never touched."""
    if current is not None and current[1] == MANUAL:
        return None
    if region_zone is None:
        return ("clear", None) if current is not None else None
    if current is not None and current[0] == region_zone:
        return None
    return ("set", region_zone)


# Regions too broad for one zone: when a member picks one, they're offered these to choose from.
BROAD_ZONES: dict[str, list[tuple[str, str]]] = {
    "asia": [
        ("Gulf (Dubai)", "Asia/Dubai"),
        ("Pakistan", "Asia/Karachi"),
        ("India", "Asia/Kolkata"),
        ("Bangladesh", "Asia/Dhaka"),
        ("Thailand, Vietnam, Indonesia (Jakarta)", "Asia/Bangkok"),
        ("China, Singapore, Philippines, Malaysia", "Asia/Singapore"),
        ("Japan, Korea", "Asia/Tokyo"),
    ],
    "south america": [
        ("Mexico (Central)", "America/Mexico_City"),
        ("Central America", "America/Guatemala"),
        ("Colombia, Peru, Ecuador", "America/Bogota"),
        ("Venezuela, Bolivia", "America/Caracas"),
        ("Chile", "America/Santiago"),
        ("Argentina, Uruguay", "America/Argentina/Buenos_Aires"),
        ("Brazil (Brasília, São Paulo)", "America/Sao_Paulo"),
    ],
}


def broad_zones_for(role_name: str) -> list[tuple[str, str]]:
    """The zones to offer someone who picked a region role that spans several."""
    w = normalise(role_name)
    if _has(w, "asia", "apac"):
        return BROAD_ZONES["asia"]
    if _has(w, "south america", "latin america", "latam", "south and central america",
            "central and south america", "south central america"):
        return BROAD_ZONES["south america"]
    return []
