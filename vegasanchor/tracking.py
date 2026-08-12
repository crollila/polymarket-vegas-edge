"""
The record-keeping half of proving edge.

Three things have to be captured, in this order, or the analysis is worthless:

  1. LOG      the price you got and what you thought it was worth, at the
              moment you decided. Recording it later means recording it with
              hindsight.
  2. CLOSE    the market's final price before kickoff. Run near game time.
              Miss this window and closing line value is gone forever -- the
              book takes the market down once the game starts.
  3. SETTLE   who actually won, from the scores feed rather than memory.

State lives in predictions.jsonl, appended never rewritten, so an interrupted
run cannot corrupt earlier records.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .analytics import Bet
from .teams import canonical_team

STORE_PATH = "predictions.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(iso: str) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


class PredictionStore:
    """Append-only JSONL log of predictions, keyed by a stable record id."""

    def __init__(self, path: str = STORE_PATH):
        self.path = path

    def _read_raw(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        rows: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def load(self) -> List[Dict[str, Any]]:
        """
        Collapse the append-only log into current state.

        Later records with the same id overwrite earlier fields, so `close` and
        `settle` are just appends rather than rewrites of history.
        """
        merged: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for row in self._read_raw():
            rid = row.get("id")
            if not rid:
                continue
            if rid not in merged:
                merged[rid] = {}
                order.append(rid)
            merged[rid].update({k: v for k, v in row.items() if v is not None})
        return [merged[i] for i in order]

    def _append(self, record: Dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def already_logged(self, market_slug: str, side: str) -> bool:
        return any(r.get("market_slug") == market_slug and r.get("side") == side
                   for r in self.load())

    def log_prediction(self, *, market_slug: str, game: str, side: str, bet_on: str,
                       cost: float, fair: float, edge: float, shares: float = 0.0,
                       stake: float = 0.0, kickoff: str = "", book_american: Optional[int] = None,
                       books: Optional[List[str]] = None, source: str = "bot",
                       paper: bool = True, teams: Optional[List[str]] = None) -> str:
        rid = uuid.uuid4().hex[:12]
        self._append({
            "id": rid, "logged_at": _now(), "market_slug": market_slug, "game": game,
            "side": side, "bet_on": bet_on, "cost": round(float(cost), 4),
            "fair": round(float(fair), 4), "edge": round(float(edge), 4),
            "shares": float(shares), "stake": float(stake), "kickoff": kickoff,
            "book_american": book_american, "books": books or [], "source": source,
            "paper": paper, "status": "open",
            # Both teams, so later stages can match on the pair. A team plays
            # many games in a season -- matching on one name alone will happily
            # grade a preseason bet against a week-3 line.
            "teams": teams or [],
        })
        return rid

    def record_close(self, rid: str, closing_fair: float,
                     closing_american: Optional[int] = None) -> None:
        self._append({"id": rid, "closing_fair": round(float(closing_fair), 4),
                      "closing_american": closing_american,
                      "closed_at": _now(), "status": "closed"})

    def record_settlement(self, rid: str, won: bool, detail: str = "") -> None:
        self._append({"id": rid, "won": bool(won), "settled_at": _now(),
                      "settle_detail": detail, "status": "settled"})

    # -- selection helpers -------------------------------------------------

    def needing_close(self, within_hours: float = 6.0) -> List[Dict[str, Any]]:
        """Open predictions whose kickoff is near enough to snapshot the close."""
        out = []
        now = datetime.now(timezone.utc)
        for r in self.load():
            if r.get("closing_fair") is not None:
                continue
            ko = _parse_ts(r.get("kickoff", ""))
            if ko is None:
                continue
            hours = (ko - now).total_seconds() / 3600.0
            if hours <= within_hours:
                out.append(r)
        return out

    def needing_settlement(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        out = []
        for r in self.load():
            if r.get("won") is not None:
                continue
            ko = _parse_ts(r.get("kickoff", ""))
            if ko is not None and ko < now:
                out.append(r)
        return out

    def to_bets(self, source: Optional[str] = None) -> List[Bet]:
        bets = []
        for r in self.load():
            if source and r.get("source") != source:
                continue
            bets.append(Bet(
                label=f"{r.get('game','?')} {r.get('side','')} {r.get('bet_on','')}",
                cost=float(r.get("cost", 0.0)),
                fair=float(r.get("fair", 0.0)),
                shares=float(r.get("shares", 0.0)),
                stake=float(r.get("stake", 0.0)),
                won=r.get("won"),
                closing_fair=r.get("closing_fair"),
                kickoff=r.get("kickoff", ""),
                source=r.get("source", "bot"),
            ))
        return bets


def settle_from_scores(store: PredictionStore, score_events: Iterable[Dict[str, Any]]) -> int:
    """
    Mark predictions won/lost using The Odds API /scores payloads.

    A bet is a win when the team you backed is the one that scored more. Games
    that are not `completed`, or that ended tied, are left open rather than
    guessed at.
    """
    finished: Dict[frozenset, Dict[str, Any]] = {}
    for ev in score_events:
        if not ev.get("completed"):
            continue
        home = canonical_team(ev.get("home_team") or "")
        away = canonical_team(ev.get("away_team") or "")
        scores = ev.get("scores") or []
        if not home or not away or len(scores) < 2:
            continue
        tally: Dict[str, float] = {}
        for s in scores:
            team = canonical_team(s.get("name") or "")
            try:
                tally[team] = float(s.get("score"))
            except (TypeError, ValueError):
                continue
        if home not in tally or away not in tally:
            continue
        if tally[home] == tally[away]:
            continue  # a tie resolves neither side cleanly; leave it open
        winner = home if tally[home] > tally[away] else away
        finished[frozenset((home, away))] = {
            "winner": winner,
            "detail": f"{away} {tally[away]:.0f} @ {home} {tally[home]:.0f}",
        }

    settled = 0
    for rec in store.needing_settlement():
        bet_on = canonical_team(rec.get("bet_on") or "")
        if not bet_on:
            continue
        match = match_by_pair(rec, finished, bet_on)
        if not match:
            continue
        store.record_settlement(rec["id"], won=(match["winner"] == bet_on),
                                detail=match["detail"])
        settled += 1
    return settled


def record_team_pair(rec: Dict[str, Any]) -> Optional[frozenset]:
    """The two canonical teams involved, when the record knows them."""
    teams = [canonical_team(t) for t in (rec.get("teams") or [])]
    teams = [t for t in teams if t]
    return frozenset(teams) if len(teams) == 2 else None


def match_by_pair(rec: Dict[str, Any], candidates: Dict[frozenset, Any],
                  bet_on: str) -> Optional[Any]:
    """
    Find this record's game among `candidates`, keyed by team pair.

    Prefers an exact both-teams match. Falls back to the single backed team
    only when the record predates pair tracking (or came from a CSV that had
    just one team), and even then refuses when it is ambiguous -- grading a bet
    against the wrong week is worse than leaving it ungraded.
    """
    pair = record_team_pair(rec)
    if pair is not None:
        return candidates.get(pair)
    hits = [v for k, v in candidates.items() if bet_on in k]
    return hits[0] if len(hits) == 1 else None
