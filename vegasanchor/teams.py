"""
Canonical NFL team registry.

The whole bot hinges on correctly deciding that Polymarket's "Detroit Lions"
and FanDuel's "Detroit Lions" are the same team -- and, more dangerously, that
"Los Angeles Chargers" and "Los Angeles Rams" are NOT. Matching on a substring
or on the last word ("Giants"/"Jets" both end in a nickname that is unique, but
"Los Angeles" alone is not) is how you end up trading the wrong side.

We resolve every incoming string to one canonical full name, or to None. None
means "skip this game" -- never guess.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# canonical full name -> (polymarket abbreviation, nickname, city aliases)
NFL_TEAMS: Dict[str, Dict[str, str]] = {
    "Arizona Cardinals":     {"abbr": "ari", "nick": "cardinals"},
    "Atlanta Falcons":       {"abbr": "atl", "nick": "falcons"},
    "Baltimore Ravens":      {"abbr": "bal", "nick": "ravens"},
    "Buffalo Bills":         {"abbr": "buf", "nick": "bills"},
    "Carolina Panthers":     {"abbr": "car", "nick": "panthers"},
    "Chicago Bears":         {"abbr": "chi", "nick": "bears"},
    "Cincinnati Bengals":    {"abbr": "cin", "nick": "bengals"},
    "Cleveland Browns":      {"abbr": "cle", "nick": "browns"},
    "Dallas Cowboys":        {"abbr": "dal", "nick": "cowboys"},
    "Denver Broncos":        {"abbr": "den", "nick": "broncos"},
    "Detroit Lions":         {"abbr": "det", "nick": "lions"},
    "Green Bay Packers":     {"abbr": "gb",  "nick": "packers"},
    "Houston Texans":        {"abbr": "hou", "nick": "texans"},
    "Indianapolis Colts":    {"abbr": "ind", "nick": "colts"},
    "Jacksonville Jaguars":  {"abbr": "jax", "nick": "jaguars"},
    "Kansas City Chiefs":    {"abbr": "kc",  "nick": "chiefs"},
    "Las Vegas Raiders":     {"abbr": "lv",  "nick": "raiders"},
    "Los Angeles Chargers":  {"abbr": "lac", "nick": "chargers"},
    "Los Angeles Rams":      {"abbr": "lar", "nick": "rams"},
    "Miami Dolphins":        {"abbr": "mia", "nick": "dolphins"},
    "Minnesota Vikings":     {"abbr": "min", "nick": "vikings"},
    "New England Patriots":  {"abbr": "ne",  "nick": "patriots"},
    "New Orleans Saints":    {"abbr": "no",  "nick": "saints"},
    "New York Giants":       {"abbr": "nyg", "nick": "giants"},
    "New York Jets":         {"abbr": "nyj", "nick": "jets"},
    "Philadelphia Eagles":   {"abbr": "phi", "nick": "eagles"},
    "Pittsburgh Steelers":   {"abbr": "pit", "nick": "steelers"},
    "San Francisco 49ers":   {"abbr": "sf",  "nick": "49ers"},
    "Seattle Seahawks":      {"abbr": "sea", "nick": "seahawks"},
    "Tampa Bay Buccaneers":  {"abbr": "tb",  "nick": "buccaneers"},
    "Tennessee Titans":      {"abbr": "ten", "nick": "titans"},
    "Washington Commanders": {"abbr": "was", "nick": "commanders"},
}

# Extra spellings seen in the wild that the generic rules below would miss.
EXTRA_ALIASES: Dict[str, str] = {
    "la chargers": "Los Angeles Chargers",
    "la rams": "Los Angeles Rams",
    "ny giants": "New York Giants",
    "ny jets": "New York Jets",
    "sf 49ers": "San Francisco 49ers",
    "niners": "San Francisco 49ers",
    "tb buccaneers": "Tampa Bay Buccaneers",
    "bucs": "Tampa Bay Buccaneers",
    "gb packers": "Green Bay Packers",
    "kc chiefs": "Kansas City Chiefs",
    "ne patriots": "New England Patriots",
    "no saints": "New Orleans Saints",
    "lv raiders": "Las Vegas Raiders",
    "oakland raiders": "Las Vegas Raiders",
    "san diego chargers": "Los Angeles Chargers",
    "st louis rams": "Los Angeles Rams",
    "washington football team": "Washington Commanders",
    "washington redskins": "Washington Commanders",
    "jacksonville jags": "Jacksonville Jaguars",
    "jags": "Jacksonville Jaguars",
    "wsh": "Washington Commanders",
    "jac": "Jacksonville Jaguars",
    "lvr": "Las Vegas Raiders",
    "sfo": "San Francisco 49ers",
    "gnb": "Green Bay Packers",
    "kan": "Kansas City Chiefs",
    "nwe": "New England Patriots",
    "nor": "New Orleans Saints",
    "tam": "Tampa Bay Buccaneers",
}


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.replace("&", "and")
    s = re.sub(r"[.\'\u2019,]", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_LOOKUP: Dict[str, str] = {}


def _register(key: str, canonical: str) -> None:
    k = _norm(key)
    if not k:
        return
    # A key that would map to two different teams is ambiguous and must not be
    # used at all -- e.g. bare "los angeles" or "new york".
    existing = _LOOKUP.get(k)
    if existing is not None and existing != canonical:
        _LOOKUP[k] = ""  # poisoned: ambiguous
        return
    _LOOKUP[k] = canonical


for _full, _meta in NFL_TEAMS.items():
    _register(_full, _full)
    _register(_meta["abbr"], _full)
    _register(_meta["nick"], _full)
    # City / region portion, e.g. "Green Bay" from "Green Bay Packers".
    _city = " ".join(_full.split()[:-1])
    _register(_city, _full)
    # Polymarket's truncated "safeName" style, e.g. "Los Angeles C".
    _register(f"{_city} {_meta['nick'][0]}", _full)

for _alias, _full in EXTRA_ALIASES.items():
    _LOOKUP[_norm(_alias)] = _full  # explicit aliases win over generated ones


def canonical_team(name: str) -> Optional[str]:
    """
    Resolve any spelling of an NFL team to its canonical full name.

    Returns None when the input is unknown or genuinely ambiguous (e.g. the
    bare string "Los Angeles", which could be the Rams or the Chargers).
    """
    if not name:
        return None
    key = _norm(name)
    if not key:
        return None

    hit = _LOOKUP.get(key)
    if hit:
        return hit
    if hit == "":
        return None  # known-ambiguous

    # "Detroit Lions Preseason", "Cincinnati Bengals (AFC)" etc: try to find a
    # unique nickname token inside the string.
    tokens = set(key.split())
    matches = {
        full for full, meta in NFL_TEAMS.items()
        if meta["nick"] in tokens or meta["abbr"] in tokens
    }
    if len(matches) == 1:
        return matches.pop()
    return None


def abbr_for(canonical: str) -> Optional[str]:
    meta = NFL_TEAMS.get(canonical)
    return meta["abbr"] if meta else None


def short_label(canonical: str) -> str:
    """'Detroit Lions' -> 'DET'. Falls back to the raw name."""
    meta = NFL_TEAMS.get(canonical)
    return meta["abbr"].upper() if meta else canonical
