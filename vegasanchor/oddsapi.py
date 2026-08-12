"""
Sportsbook odds via The Odds API (v4).

We do NOT scrape FanDuel. Scraping a sportsbook is brittle and against their
terms; The Odds API resells the same prices through a documented feed. Set
ODDS_API_BOOKMAKERS=fanduel to anchor on FanDuel alone, or list several books
to anchor on their median (steadier, and usually closer to the true price).

Quota note: the free tier is 500 requests/month. One scan costs one request
per sport key, and /v4/sports (used for season discovery) is free. The
remaining quota is reported back on every call.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from .devig import american_to_prob, devig, hold_pct
from .teams import canonical_team

API_BASE = "https://api.the-odds-api.com/v4"


class OddsApiError(RuntimeError):
    pass


@dataclass
class BookLine:
    """One book's two-way moneyline on one game."""
    book: str
    american: Dict[str, int]          # canonical team -> American odds
    hold: float = 0.0


@dataclass
class GameOdds:
    """Consensus sportsbook view of a single NFL game."""
    event_id: str
    sport_key: str
    commence_time: str
    home_team: str                    # canonical
    away_team: str                    # canonical
    lines: List[BookLine] = field(default_factory=list)
    fair: Dict[str, float] = field(default_factory=dict)   # canonical team -> devigged prob
    books_used: List[str] = field(default_factory=list)

    @property
    def teams(self) -> frozenset:
        return frozenset((self.home_team, self.away_team))

    def american_for(self, team: str) -> Optional[int]:
        """Representative (median) American price for a team, for display."""
        vals = [ln.american[team] for ln in self.lines if team in ln.american]
        if not vals:
            return None
        return int(statistics.median(vals))


class OddsApiClient:
    def __init__(self, api_key: str, bookmakers: Sequence[str] = ("fanduel",),
                 region: str = "us", timeout: int = 25):
        if not api_key or api_key.startswith("your_"):
            raise OddsApiError(
                "ODDS_API_KEY is missing or still the placeholder. "
                "Get a free key at https://the-odds-api.com and put it in .env"
            )
        self.api_key = api_key
        self.bookmakers = [b.strip().lower() for b in bookmakers if b.strip()]
        self.region = region
        self.timeout = timeout
        self.session = requests.Session()
        self.requests_remaining: Optional[str] = None
        self.requests_used: Optional[str] = None

    def _get(self, path: str, params: Dict[str, object]) -> object:
        params = {"apiKey": self.api_key, **params}
        r = self.session.get(f"{API_BASE}{path}", params=params, timeout=self.timeout)
        self.requests_remaining = r.headers.get("x-requests-remaining", self.requests_remaining)
        self.requests_used = r.headers.get("x-requests-used", self.requests_used)
        if r.status_code == 401:
            raise OddsApiError("The Odds API rejected the key (401). Check ODDS_API_KEY.")
        if r.status_code == 429:
            raise OddsApiError("The Odds API quota exhausted (429). Free tier is 500/month.")
        if r.status_code >= 400:
            raise OddsApiError(f"The Odds API HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def nfl_sport_keys(self) -> List[str]:
        """
        Discover which NFL feeds are in season right now.

        In August that is usually americanfootball_nfl_preseason; from September
        it is americanfootball_nfl. Hardcoding one key means the scanner silently
        returns nothing for months. This endpoint does not consume quota.
        """
        sports = self._get("/sports", {"all": "false"})
        keys = []
        for s in sports:
            if not isinstance(s, dict) or not s.get("active"):
                continue
            key = s.get("key", "")
            if not key.startswith("americanfootball_nfl"):
                continue
            # Outright/futures feeds (super bowl winner, conference winner) carry
            # no h2h market, so fetching them burns a request out of a 500/month
            # budget and returns nothing this bot can use.
            if any(tag in key for tag in ("winner", "champion", "mvp", "odds_")):
                continue
            keys.append(key)
        return keys or ["americanfootball_nfl"]

    def fetch_nfl_odds(self, sport_keys: Optional[Sequence[str]] = None,
                       devig_method: str = "power") -> List[GameOdds]:
        keys = list(sport_keys) if sport_keys else self.nfl_sport_keys()
        games: List[GameOdds] = []
        for key in keys:
            params: Dict[str, object] = {
                "markets": "h2h",
                "oddsFormat": "american",
                "dateFormat": "iso",
            }
            if self.bookmakers:
                params["bookmakers"] = ",".join(self.bookmakers)
            else:
                params["regions"] = self.region
            try:
                raw = self._get(f"/sports/{key}/odds", params)
            except OddsApiError:
                if len(keys) == 1:
                    raise
                continue
            for ev in raw or []:
                parsed = self._parse_event(ev, key, devig_method)
                if parsed:
                    games.append(parsed)
        return games

    @staticmethod
    def _parse_event(ev: dict, sport_key: str, devig_method: str) -> Optional[GameOdds]:
        home = canonical_team(ev.get("home_team") or "")
        away = canonical_team(ev.get("away_team") or "")
        if not home or not away or home == away:
            return None

        game = GameOdds(
            event_id=ev.get("id") or "",
            sport_key=sport_key,
            commence_time=ev.get("commence_time") or "",
            home_team=home,
            away_team=away,
        )

        for bm in ev.get("bookmakers", []) or []:
            h2h = next((m for m in bm.get("markets", []) or [] if m.get("key") == "h2h"), None)
            if not h2h:
                continue
            priced: Dict[str, int] = {}
            for o in h2h.get("outcomes", []) or []:
                team = canonical_team(o.get("name") or "")
                price = o.get("price")
                if team in (home, away) and isinstance(price, (int, float)):
                    priced[team] = int(price)
            # A one-sided quote cannot be devigged; drop it.
            if len(priced) == 2:
                game.lines.append(BookLine(
                    book=bm.get("key") or "?",
                    american=priced,
                    hold=hold_pct(priced[home], priced[away]),
                ))

        if not game.lines:
            return None

        # Median raw implied probability across books, then remove the vig once.
        raw_home = statistics.median(
            american_to_prob(ln.american[home]) for ln in game.lines
        )
        raw_away = statistics.median(
            american_to_prob(ln.american[away]) for ln in game.lines
        )
        fair_home, fair_away = devig([raw_home, raw_away], method=devig_method)
        game.fair = {home: fair_home, away: fair_away}
        game.books_used = [ln.book for ln in game.lines]
        return game


def fetch_spreads(client: "OddsApiClient", sport_key: str) -> Dict[frozenset, Dict[str, float]]:
    """
    Pregame point spreads, as {team_pair: {team: expected_margin}}.

    The spread IS the market's expected margin, which is exactly the drift term
    an in-game win-probability model needs. Without it the model assumes both
    teams were even before tip-off, and a heavy underdog trailing looks like a
    bargain when it is simply losing as expected.

    Sign convention: a team laying 6.5 has point -6.5 in the feed and an
    expected margin of +6.5.
    """
    params: Dict[str, object] = {"markets": "spreads", "oddsFormat": "american",
                                 "dateFormat": "iso"}
    if client.bookmakers:
        params["bookmakers"] = ",".join(client.bookmakers)
    else:
        params["regions"] = client.region
    try:
        raw = client._get(f"/sports/{sport_key}/odds", params)
    except OddsApiError:
        return {}

    out: Dict[frozenset, Dict[str, float]] = {}
    for ev in raw or []:
        home = canonical_team(ev.get("home_team") or "") or (ev.get("home_team") or "")
        away = canonical_team(ev.get("away_team") or "") or (ev.get("away_team") or "")
        if not home or not away:
            continue
        margins: Dict[str, List[float]] = {}
        for bm in ev.get("bookmakers", []) or []:
            mk = next((m for m in bm.get("markets", []) or []
                       if m.get("key") == "spreads"), None)
            if not mk:
                continue
            for o in mk.get("outcomes", []) or []:
                nm = canonical_team(o.get("name") or "") or (o.get("name") or "")
                pt = o.get("point")
                if nm and isinstance(pt, (int, float)):
                    margins.setdefault(nm, []).append(-float(pt))
        if margins:
            out[frozenset((home, away))] = {
                k: statistics.median(v) for k, v in margins.items()
            }
    return out


def _loose(s: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def _names_match(a: str, b: str) -> bool:
    """
    Do two team strings refer to the same team?

    Feeds disagree on how much of a name to print: Polymarket says
    "Washington", The Odds API says "Washington Mystics". Neither equality nor
    a shared last word works across leagues, so we accept a prefix/containment
    match on whole words.
    """
    x, y = _loose(a), _loose(b)
    if not x or not y:
        return False
    if x == y:
        return True
    xs, ys = x.split(), y.split()
    short, long_ = (xs, ys) if len(xs) <= len(ys) else (ys, xs)
    return long_[:len(short)] == short


def match_spread(yes_team: str, no_team: str,
                 spreads: Dict[frozenset, Dict[str, float]]
                 ) -> Optional[Dict[str, float]]:
    """
    Find the pregame spread for a live game, re-keyed to the caller's names.

    Requires a unique, unambiguous pairing. If a short name like "Los Angeles"
    could refer to either of two teams in the feed, this returns None rather
    than guessing -- attaching the wrong line to a live game is worse than
    running without one.
    """
    hits = []
    for pair, margins in spreads.items():
        names = list(pair)
        if len(names) != 2:
            continue
        for a, b in ((names[0], names[1]), (names[1], names[0])):
            if _names_match(yes_team, a) and _names_match(no_team, b):
                if a in margins and b in margins:
                    hits.append({yes_team: margins[a], no_team: margins[b]})
                break
    return hits[0] if len(hits) == 1 else None


def index_by_teams(games: Sequence[GameOdds]) -> Dict[frozenset, GameOdds]:
    """Index games by their unordered team pair for O(1) matching."""
    out: Dict[frozenset, GameOdds] = {}
    for g in games:
        out[g.teams] = g
    return out
