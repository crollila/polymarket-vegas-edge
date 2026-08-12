"""
track.py -- prove (or disprove) the edge.

    python track.py log                 # snapshot today's recommendations
    python track.py close               # capture closing lines before kickoff
    python track.py settle              # mark winners from the scores feed
    python track.py report              # CLV, calibration, ROI with a CI
    python track.py import bets.csv     # grade a record of bets you already made

Typical loop: `log` whenever you scan, `close` shortly before kickoff, `settle`
the next morning, `report` whenever you want to know if any of it is working.
"""

from __future__ import annotations

import argparse
import csv
import sys
from typing import List, Optional

from vegasanchor.analytics import Bet, summarize
from vegasanchor.config import Config
from vegasanchor.devig import american_to_prob, prob_to_american
from vegasanchor.edge import scan
from vegasanchor.oddsapi import OddsApiClient, OddsApiError, index_by_teams
from vegasanchor.polymarket import GatewayClient, TradeClient
from vegasanchor.teams import canonical_team, short_label
from vegasanchor.tracking import PredictionStore, match_by_pair, settle_from_scores


# --------------------------------------------------------------------------
# log
# --------------------------------------------------------------------------
def cmd_log(args, cfg: Config) -> int:
    store = PredictionStore(args.store)
    try:
        odds_client = OddsApiClient(cfg.odds_api_key, cfg.bookmakers, cfg.odds_region)
        games = odds_client.fetch_nfl_odds(devig_method=cfg.devig_method)
    except OddsApiError as e:
        print(f"Odds feed unavailable: {e}", file=sys.stderr)
        return 2

    gw = GatewayClient()
    by_teams = index_by_teams(games)
    markets = [m for m in gw.nfl_moneyline_markets(include_live=cfg.include_live)
               if frozenset((m.yes_team, m.no_team)) in by_teams]
    priced = [m for m in markets if gw.load_bbo(m)]

    bankroll = args.bankroll or 1000.0
    recs = scan(priced, by_teams, cfg, bankroll)

    logged = 0
    for r in recs:
        if store.already_logged(r.market.market_slug, r.side):
            continue
        store.log_prediction(
            market_slug=r.market.market_slug, game=r.market.label(), side=r.side,
            bet_on=r.team, cost=r.cost, fair=r.fair, edge=r.edge,
            shares=r.shares or round(bankroll * 0.01 / max(r.cost, 0.01), 2),
            stake=r.stake_usd or round(bankroll * 0.01, 2),
            kickoff=r.market.kickoff, book_american=r.book_american,
            books=r.game.books_used, source="bot", paper=True,
            teams=[r.market.yes_team, r.market.no_team],
        )
        logged += 1
        print(f"  logged {r.market.label():<14} {r.action:<20} @ {r.cost:.2f} "
              f"(fair {r.fair:.2f}, edge {r.edge_pct:+.1f}%)")

    print(f"\n{logged} new prediction(s) recorded in {args.store} "
          f"({len(recs)} qualifying, {len(recs) - logged} already tracked).")
    if not recs:
        print("Nothing cleared the edge threshold. Lower it with --min-edge to "
              "log more samples; calibration needs volume, not just winners.")
    return 0


# --------------------------------------------------------------------------
# close
# --------------------------------------------------------------------------
def cmd_close(args, cfg: Config) -> int:
    store = PredictionStore(args.store)
    pending = store.needing_close(within_hours=args.within)
    if not pending:
        print("No predictions are close enough to kickoff to snapshot.")
        return 0

    try:
        odds_client = OddsApiClient(cfg.odds_api_key, cfg.bookmakers, cfg.odds_region)
        games = odds_client.fetch_nfl_odds(devig_method=cfg.devig_method)
    except OddsApiError as e:
        print(f"Odds feed unavailable: {e}", file=sys.stderr)
        return 2

    by_teams = index_by_teams(games)
    captured = 0
    ambiguous = 0
    for rec in pending:
        team = canonical_team(rec.get("bet_on") or "")
        if not team:
            continue
        game = match_by_pair(rec, by_teams, team)
        if game is None:
            ambiguous += 1
            continue
        if team not in game.fair:
            continue
        closing = game.fair[team]
        store.record_close(rec["id"], closing, game.american_for(team))
        clv = closing - float(rec.get("cost", 0.0))
        captured += 1
        print(f"  {rec.get('game','?'):<14} {short_label(team):<4} "
              f"paid {float(rec.get('cost',0)):.2f} -> close {closing:.2f}  "
              f"CLV {clv*100:+.1f} pts")

    print(f"\nCaptured {captured} closing line(s).")
    if ambiguous:
        print(f"{ambiguous} skipped: could not identify the fixture unambiguously. "
              "A team plays many games a season, so a record without both teams "
              "is left ungraded rather than matched to the wrong week.")
    leftover = len(pending) - captured - ambiguous
    if leftover > 0:
        print(f"{leftover} unmatched -- the book has likely pulled the game. "
              "Closing lines cannot be recovered after kickoff.")
    return 0


# --------------------------------------------------------------------------
# settle
# --------------------------------------------------------------------------
def cmd_settle(args, cfg: Config) -> int:
    store = PredictionStore(args.store)
    pending = store.needing_settlement()
    if not pending:
        print("Nothing awaiting settlement.")
        return 0
    try:
        client = OddsApiClient(cfg.odds_api_key, cfg.bookmakers, cfg.odds_region)
        events: List[dict] = []
        for key in client.nfl_sport_keys():
            events.extend(client._get(f"/sports/{key}/scores", {"daysFrom": args.days_from}) or [])
    except OddsApiError as e:
        print(f"Scores feed unavailable: {e}", file=sys.stderr)
        return 2

    n = settle_from_scores(store, events)
    print(f"Settled {n} of {len(pending)} pending prediction(s).")
    if n < len(pending):
        print(f"{len(pending)-n} still open -- either not finished, or older than "
              f"the {args.days_from}-day scores window (free tier caps this at 3).")
    return 0


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------
REQUIRED_HELP = """
Expected CSV columns (header row required; extra columns are ignored):

  date            game date, any ISO-ish format          2025-11-09
  bet_on          team you backed                        Detroit Lions
  odds            American odds you got                  -150
  stake           dollars risked                         100
  opponent        the other team (strongly recommended;
                  without it a bet cannot be matched to
                  a specific week)                       Cincinnati Bengals
  result          win / loss / push  (optional if you
                  then run `settle`)                     win
  closing_odds    American closing line (optional, but
                  this is what unlocks CLV)              -170
  fair            your own probability estimate at bet
                  time (optional, unlocks calibration)   0.63
"""


def _parse_result(v: str) -> Optional[bool]:
    s = (v or "").strip().lower()
    if s in ("win", "w", "won", "1", "true", "yes"):
        return True
    if s in ("loss", "l", "lost", "lose", "0", "false", "no"):
        return False
    return None


def cmd_import(args, cfg: Config) -> int:
    store = PredictionStore(args.store)
    try:
        f = open(args.csv, newline="", encoding="utf-8-sig")
    except OSError as e:
        print(f"Cannot open {args.csv}: {e}", file=sys.stderr)
        print(REQUIRED_HELP)
        return 2

    imported = skipped = 0
    problems: List[str] = []
    with f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
        missing = [c for c in ("bet_on", "odds", "stake") if c not in cols]
        if missing:
            print(f"CSV is missing required column(s): {', '.join(missing)}", file=sys.stderr)
            print(f"Found: {', '.join(reader.fieldnames or [])}")
            print(REQUIRED_HELP)
            return 2

        def get(row, name, default=""):
            return (row.get(cols[name], default) if name in cols else default) or default

        for i, row in enumerate(reader, start=2):
            team = canonical_team(get(row, "bet_on"))
            if not team:
                problems.append(f"row {i}: unrecognized team {get(row,'bet_on')!r}")
                skipped += 1
                continue
            try:
                american = int(float(get(row, "odds")))
                stake = float(get(row, "stake"))
            except ValueError:
                problems.append(f"row {i}: bad odds/stake")
                skipped += 1
                continue
            if american == 0 or stake <= 0:
                problems.append(f"row {i}: odds of 0 or non-positive stake")
                skipped += 1
                continue

            # An American-odds wager maps exactly onto a prediction-market
            # contract: cost per share is the implied probability, and the
            # share count is whatever that stake buys.
            cost = american_to_prob(american)
            shares = stake / cost

            fair_raw = get(row, "fair")
            try:
                fair = float(fair_raw) if fair_raw else cost
            except ValueError:
                fair = cost

            rid = store.log_prediction(
                market_slug=f"manual-{i}", game=get(row, "date") or "manual",
                side="YES", bet_on=team, cost=cost, fair=fair,
                edge=fair - cost, shares=shares, stake=stake,
                kickoff=get(row, "date"), book_american=american,
                books=["manual"], source="manual", paper=False,
                teams=[t for t in (team, canonical_team(get(row, "opponent"))) if t],
            )

            closing_raw = get(row, "closing_odds")
            if closing_raw:
                try:
                    ca = int(float(closing_raw))
                    if ca != 0:
                        store.record_close(rid, american_to_prob(ca), ca)
                except ValueError:
                    pass

            res = _parse_result(get(row, "result"))
            if res is not None:
                store.record_settlement(rid, res, detail="imported")

            imported += 1

    print(f"Imported {imported} bet(s), skipped {skipped}.")
    for p in problems[:15]:
        print(f"  {p}")
    if len(problems) > 15:
        print(f"  ... and {len(problems)-15} more")
    if imported:
        print(f"\nRun:  python track.py report --source manual")
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def _bar(frac: float, width: int = 24) -> str:
    n = max(0, min(width, int(round(frac * width))))
    return "#" * n + "." * (width - n)


def cmd_report(args, cfg: Config) -> int:
    store = PredictionStore(args.store)
    bets: List[Bet] = store.to_bets(source=args.source)
    if not bets:
        where = f" for source={args.source}" if args.source else ""
        print(f"No predictions recorded{where}. Run `python track.py log` first, "
              f"or import a record with `python track.py import bets.csv`.")
        return 0

    s = summarize(bets)
    scope = args.source or "all"
    print(f"\n{'='*66}\n  TRACK RECORD -- source: {scope}\n{'='*66}")
    print(f"  predictions {s['total']}   settled {s['settled']}   pending {s['pending']}")

    # -- P&L --------------------------------------------------------------
    if s["settled"]:
        print(f"\n  RETURNS")
        print(f"    staked        ${s['staked']:,.2f}")
        print(f"    profit        ${s['profit']:+,.2f}")
        if s["roi"] is not None:
            print(f"    ROI           {s['roi']*100:+.2f}%")
        if s["hit_rate"] is not None:
            print(f"    hit rate      {s['hit_rate']*100:.1f}%")
        ci = s["roi_ci"]
        if ci:
            lo, hi = ci
            print(f"    95% CI on ROI [{lo*100:+.1f}%, {hi*100:+.1f}%]  (bootstrap, 10k resamples)")
            if lo <= 0 <= hi:
                print(f"      -> spans zero: this record does NOT yet distinguish "
                      f"skill from luck.")
            elif lo > 0:
                print(f"      -> entirely above zero: profitable beyond plausible variance.")
        if s["n_for_significance"]:
            print(f"    bets needed for t=2 at this ROI and variance: "
                  f"~{s['n_for_significance']:,}")

    # -- CLV: the real signal ---------------------------------------------
    clv = s["clv"]
    print(f"\n  CLOSING LINE VALUE")
    if not clv:
        print("    no closing lines captured. This is the single most valuable")
        print("    number here -- run `python track.py close` before kickoff.")
    else:
        print(f"    n             {clv['n']}")
        print(f"    mean CLV      {clv['mean']*100:+.2f} probability points")
        print(f"    beat close    {clv['beat_rate']*100:.1f}% of the time")
        print(f"    t-stat        {clv['t_stat']:+.2f}")
        if clv["n"] < 20:
            print("      -> too few to conclude anything yet.")
        elif clv["t_stat"] > 2:
            print("      -> beating the close at t>2. This is real evidence of edge.")
        elif clv["t_stat"] < -2:
            print("      -> LOSING to the close consistently. The signal is behind")
            print("         the market, not ahead of it.")
        else:
            print("      -> indistinguishable from no edge so far.")

    # -- Calibration -------------------------------------------------------
    cal = s["calibration"]
    if cal:
        print(f"\n  CALIBRATION  (does '70%' happen 70% of the time?)")
        print(f"    {'bucket':<12}{'n':>5}{'predicted':>11}{'actual':>9}   reliability")
        for row in cal:
            print(f"    {row['lo']:.1f}-{row['hi']:.1f}   {row['n']:>5}"
                  f"{row['predicted']*100:>10.1f}%{row['actual']*100:>8.1f}%   "
                  f"{_bar(row['actual'])}")
        if s["brier"] is not None:
            print(f"\n    Brier         {s['brier']:.4f}  (lower is better)")
        if s["brier_skill"] is not None:
            bss = s["brier_skill"]
            print(f"    Brier skill   {bss:+.4f}  "
                  f"({'beats' if bss > 0 else 'worse than'} always guessing the base rate)")
        if s["log_loss"] is not None:
            print(f"    log loss      {s['log_loss']:.4f}")

    # -- Interpretation ----------------------------------------------------
    print(f"\n{'='*66}")
    if s["settled"] < 30:
        print("  Sample is small. Fewer than ~30 settled bets tells you almost")
        print("  nothing about ROI -- lean on CLV, which converges much faster.")
    print(f"{'='*66}\n")
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Track and grade the bot's predictions.")
    ap.add_argument("--store", default="predictions.jsonl", help="prediction log path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("log", help="record current recommendations")
    p.add_argument("--min-edge", type=float)
    p.add_argument("--bankroll", type=float)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("close", help="capture closing lines before kickoff")
    p.add_argument("--within", type=float, default=6.0, help="hours before kickoff")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("settle", help="mark winners from the scores feed")
    p.add_argument("--days-from", type=int, default=3, help="scores lookback (free tier max 3)")
    p.set_defaults(fn=cmd_settle)

    p = sub.add_parser("import", help="import a CSV of bets you already placed")
    p.add_argument("csv")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("report", help="CLV, calibration and ROI")
    p.add_argument("--source", choices=["bot", "manual"], help="filter by origin")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    cfg = Config.from_env(min_edge=getattr(args, "min_edge", None))
    return args.fn(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
