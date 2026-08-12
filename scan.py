"""
scan.py -- read-only. Prints what to buy and why. Places no orders, ever.

    python scan.py                     # FanDuel vs Polymarket, ranked edges
    python scan.py --all               # show every matched game, not just bets
    python scan.py --books fanduel,draftkings,betmgm
    python scan.py --min-edge 0.02 --bankroll 100
    python scan.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List

from vegasanchor.config import Config
from vegasanchor.devig import prob_to_american
from vegasanchor.edge import Recommendation, evaluate, scan, size_recommendation
from vegasanchor.oddsapi import OddsApiClient, OddsApiError, index_by_teams
from vegasanchor.polymarket import GatewayClient, TradeClient
from vegasanchor.teams import short_label


def _fmt_american(v) -> str:
    if v is None:
        return "  n/a"
    return f"{v:+d}"


def print_table(recs: List[Recommendation], title: str) -> None:
    if not recs:
        return
    print(f"\n{title}")
    print("-" * 104)
    print(f"{'GAME':<14}{'ACTION':<20}{'PM COST':>8}{'BOOK':>8}{'FAIR':>7}"
          f"{'EDGE':>8}{'STAKE':>10}{'SHARES':>8}{'EV':>8}  KICKOFF")
    print("-" * 104)
    for r in recs:
        kickoff = (r.market.kickoff or "")[:16].replace("T", " ")
        live = " LIVE" if r.market.live else ""
        stake = f"${r.stake_usd:,.2f}" if r.shares else "   --"
        shares = str(r.shares) if r.shares else "--"
        ev = f"${r.expected_value_usd:+.2f}" if r.shares else "  --"
        print(f"{r.market.label():<14}{r.action:<20}{r.cost:>8.2f}"
              f"{_fmt_american(r.book_american):>8}{r.fair:>7.2f}"
              f"{r.edge_pct:>+7.1f}%{stake:>10}{shares:>8}{ev:>8}  {kickoff}{live}")
    print("-" * 104)


def main() -> int:
    ap = argparse.ArgumentParser(description="Find NFL edges between FanDuel and Polymarket.")
    ap.add_argument("--books", help="comma-separated bookmaker keys (default: from .env, else fanduel)")
    ap.add_argument("--min-edge", type=float, help="minimum edge, e.g. 0.03 for 3 cents")
    ap.add_argument("--kelly", type=float, help="Kelly fraction (default 0.25)")
    ap.add_argument("--bankroll", type=float, help="override bankroll instead of reading your balance")
    ap.add_argument("--devig", choices=["power", "multiplicative"], help="vig removal method")
    ap.add_argument("--max-spread", type=float, help="skip markets wider than this")
    ap.add_argument("--no-live", action="store_true", help="exclude in-progress games")
    ap.add_argument("--maker", action="store_true", help="price as a resting maker order")
    ap.add_argument("--all", action="store_true", help="show every matched game, including no-bets")
    ap.add_argument("--json", metavar="PATH", help="also write results as JSON")
    args = ap.parse_args()

    cfg = Config.from_env(
        min_edge=args.min_edge,
        kelly_fraction=args.kelly,
        devig_method=args.devig,
        max_spread=args.max_spread,
        maker_only=True if args.maker else None,
        include_live=False if args.no_live else None,
    )
    if args.books:
        cfg.bookmakers = [b.strip().lower() for b in args.books.split(",") if b.strip()]

    # --- Bankroll -------------------------------------------------------
    bankroll = args.bankroll
    if bankroll is None:
        try:
            bankroll = TradeClient(cfg.pm_api_key_id, cfg.pm_private_key_b64).buying_power()
            print(f"Polymarket buying power: ${bankroll:,.2f}")
        except Exception as e:
            bankroll = 100.0
            print(f"Could not read balance ({e}); assuming ${bankroll:,.2f} for sizing.", file=sys.stderr)

    # --- Sportsbook -----------------------------------------------------
    try:
        odds_client = OddsApiClient(cfg.odds_api_key, cfg.bookmakers, cfg.odds_region)
        keys = odds_client.nfl_sport_keys()
        games = odds_client.fetch_nfl_odds(keys, devig_method=cfg.devig_method)
    except OddsApiError as e:
        print(f"\nSportsbook feed unavailable: {e}", file=sys.stderr)
        return 2

    print(f"Books: {','.join(cfg.bookmakers) or cfg.odds_region} | "
          f"feeds: {','.join(keys)} | games: {len(games)} | "
          f"devig: {cfg.devig_method} | quota left: {odds_client.requests_remaining}")

    # --- Polymarket -----------------------------------------------------
    gw = GatewayClient()
    markets = gw.nfl_moneyline_markets(include_live=cfg.include_live)
    print(f"Polymarket NFL moneyline markets: {len(markets)}")

    by_teams = index_by_teams(games)
    matched = [m for m in markets if frozenset((m.yes_team, m.no_team)) in by_teams]
    print(f"Matched to a sportsbook line: {len(matched)}")
    if not matched:
        print("\nNo overlap between the two sources right now. This is normal "
              "outside of a game week -- Polymarket lists futures long before "
              "books post moneylines.")
        return 0

    priced = [m for m in matched if gw.load_bbo(m)]
    print(f"With a live two-sided book: {len(priced)}")

    recs = scan(priced, by_teams, cfg, bankroll)

    print_table(recs, f"BETS  (edge >= {cfg.min_edge:.1%} after a {cfg.fee_buffer:.1%} fee buffer)")

    if args.all:
        # Re-score with no edge floor so you can see the near-misses too.
        loose = Config.from_env(min_edge=-1.0, devig_method=cfg.devig_method,
                                max_spread=cfg.max_spread, fee_buffer=cfg.fee_buffer)
        loose.maker_only = cfg.maker_only
        loose.min_open_interest = cfg.min_open_interest
        others = []
        for m in priced:
            g = by_teams.get(frozenset((m.yes_team, m.no_team)))
            r = evaluate(m, g, loose) if g else None
            if r and not any(x.market.market_slug == r.market.market_slug for x in recs):
                others.append(size_recommendation(r, bankroll, loose))
        others.sort(key=lambda r: r.edge, reverse=True)
        print_table(others, "NO BET  (best side shown, edge below threshold)")

    if not recs:
        print("\nNo qualifying edges. That is the normal outcome most of the time --\n"
              "a liquid market agreeing with the book means there is nothing to do.")
    else:
        total = sum(r.stake_usd for r in recs)
        ev = sum(r.expected_value_usd for r in recs)
        print(f"\n{len(recs)} bet(s) | total stake ${total:,.2f} | "
              f"modeled EV ${ev:+,.2f} | bankroll ${bankroll:,.2f}")
        unfunded = [r for r in recs if r.shares == 0]
        if unfunded:
            print(f"{len(unfunded)} edge(s) found but sized below the "
                  f"${cfg.min_order_usd:.0f} exchange minimum -- bankroll too small to act.")
        print("\nRead as: 'BUY NO on CIN' = buy the NO contract, which pays if "
              "Cincinnati does NOT win.")

    if args.json:
        payload = [{
            "market_slug": r.market.market_slug,
            "game": r.market.label(),
            "side": r.side,
            "bet_on": r.team,
            "cost": round(r.cost, 4),
            "fair_prob": round(r.fair, 4),
            "fair_american": prob_to_american(r.fair),
            "book_american": r.book_american,
            "edge": round(r.edge, 4),
            "stake_usd": r.stake_usd,
            "shares": r.shares,
            "limit_yes_price": r.limit_yes_price,
            "intent": r.intent,
            "kickoff": r.market.kickoff,
            "live": r.market.live,
            "books": r.game.books_used,
        } for r in recs]
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"bankroll": bankroll, "recommendations": payload}, f, indent=2)
        print(f"Wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
