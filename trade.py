"""
trade.py -- the bot loop. DRY RUN unless you pass --live.

    python trade.py                 # scan + preview real orders, submit nothing
    python trade.py --once          # single pass, then exit
    python trade.py --live          # actually submit orders (asks you to confirm)

Safety rails, all of which must pass before anything is submitted:
  * --live flag AND an interactive typed confirmation
  * a per-trade cap and a fractional-Kelly stake
  * a cap on simultaneous open positions
  * one order per market per session (no averaging into a losing thesis)
  * a KILL_SWITCH file: create it and the loop stops before its next order
  * every decision is appended to trade_journal.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Dict, Set

from vegasanchor.config import Config
from vegasanchor.edge import build_order_payload, scan
from vegasanchor.oddsapi import OddsApiClient, OddsApiError, index_by_teams
from vegasanchor.polymarket import GatewayClient, TradeClient, open_position_count

_STOP = {"flag": False}


def _handle_signal(signum, frame):
    _STOP["flag"] = True
    print("\nStopping after the current cycle...")


def journal(path: str, event: Dict[str, object]) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def confirm_live(cfg: Config, bankroll: float) -> bool:
    print("\n" + "=" * 68)
    print("  LIVE TRADING -- this will spend real money on your Polymarket US")
    print(f"  account. Buying power: ${bankroll:,.2f}")
    print(f"  Max per trade: {cfg.max_pct_bankroll_per_trade:.0%} "
          f"(${bankroll * cfg.max_pct_bankroll_per_trade:,.2f})  |  "
          f"Kelly: {cfg.kelly_fraction:.2f}x  |  Min edge: {cfg.min_edge:.1%}")
    print("=" * 68)
    if not sys.stdin.isatty():
        print("Refusing to trade live from a non-interactive session.")
        return False
    try:
        answer = input('Type "TRADE" to authorize this session: ')
    except (EOFError, KeyboardInterrupt):
        # isatty() is not reliable everywhere (Git Bash on Windows reports a
        # redirected stdin as a tty), so treat an unreadable prompt as a no.
        print("\nNo confirmation received; refusing to trade live.")
        return False
    return answer.strip() == "TRADE"


def run_cycle(cfg: Config, gw: GatewayClient, trader: TradeClient,
              odds_client: OddsApiClient, traded: Set[str]) -> None:
    if os.path.exists(cfg.kill_switch_file):
        print(f"Kill switch present ({cfg.kill_switch_file}); not trading.")
        _STOP["flag"] = True
        return

    try:
        bankroll = trader.buying_power()
        pos_count = open_position_count(trader.positions())
    except Exception as e:
        print(f"Account read failed: {e}")
        return

    if pos_count >= cfg.max_positions:
        print(f"Position cap reached ({pos_count}/{cfg.max_positions}); holding.")
        return

    try:
        games = odds_client.fetch_nfl_odds(devig_method=cfg.devig_method)
    except OddsApiError as e:
        print(f"Odds feed error: {e}")
        return

    markets = gw.nfl_moneyline_markets(include_live=cfg.include_live)
    by_teams = index_by_teams(games)
    priced = [
        m for m in markets
        if frozenset((m.yes_team, m.no_team)) in by_teams
        and m.market_slug not in traded
        and gw.load_bbo(m)
    ]
    recs = scan(priced, by_teams, cfg, bankroll)

    print(f"[{time.strftime('%H:%M:%S')}] bankroll ${bankroll:,.2f} | "
          f"positions {pos_count}/{cfg.max_positions} | games {len(games)} | "
          f"priced {len(priced)} | edges {len(recs)} | quota {odds_client.requests_remaining}")

    room = cfg.max_positions - pos_count
    for rec in recs[:room]:
        if _STOP["flag"] or os.path.exists(cfg.kill_switch_file):
            return
        if rec.shares <= 0:
            print(f"  {rec.market.label()}: edge {rec.edge_pct:+.1f}% but stake "
                  f"below the ${cfg.min_order_usd:.0f} minimum; skipping.")
            continue

        payload = build_order_payload(rec, cfg)
        print(f"  {rec.describe()}")

        try:
            preview = trader.preview_order(payload)
        except Exception as e:
            print(f"    preview rejected: {e}")
            journal(cfg.journal_path, {"type": "preview_failed", "payload": payload, "error": str(e)})
            continue

        journal(cfg.journal_path, {
            "type": "candidate", "dry_run": cfg.dry_run,
            "game": rec.market.label(), "side": rec.side, "bet_on": rec.team,
            "cost": rec.cost, "fair": rec.fair, "edge": rec.edge,
            "payload": payload, "preview": preview,
        })

        if cfg.dry_run:
            print("    DRY RUN -- preview accepted, no order submitted.")
            traded.add(rec.market.market_slug)
            continue

        try:
            resp = trader.create_order(payload)
            print(f"    ORDER SUBMITTED: {resp}")
            journal(cfg.journal_path, {"type": "order_submitted", "payload": payload, "response": resp})
            traded.add(rec.market.market_slug)
        except Exception as e:
            print(f"    submit failed: {e}")
            journal(cfg.journal_path, {"type": "submit_failed", "payload": payload, "error": str(e)})


def main() -> int:
    ap = argparse.ArgumentParser(description="Vegas-anchored Polymarket NFL bot.")
    ap.add_argument("--live", action="store_true", help="submit real orders (default: dry run)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--min-edge", type=float)
    ap.add_argument("--kelly", type=float)
    ap.add_argument("--max-positions", type=int)
    ap.add_argument("--interval", type=int, help="seconds between cycles")
    ap.add_argument("--maker", action="store_true", help="rest orders instead of crossing")
    ap.add_argument("--books", help="comma-separated bookmaker keys")
    args = ap.parse_args()

    cfg = Config.from_env(
        min_edge=args.min_edge,
        kelly_fraction=args.kelly,
        max_positions=args.max_positions,
        loop_seconds=args.interval,
        maker_only=True if args.maker else None,
        dry_run=not args.live,
    )
    if args.books:
        cfg.bookmakers = [b.strip().lower() for b in args.books.split(",") if b.strip()]

    try:
        trader = TradeClient(cfg.pm_api_key_id, cfg.pm_private_key_b64)
        odds_client = OddsApiClient(cfg.odds_api_key, cfg.bookmakers, cfg.odds_region)
    except (ValueError, OddsApiError) as e:
        print(f"Startup failed: {e}", file=sys.stderr)
        return 2

    try:
        bankroll = trader.buying_power()
    except Exception as e:
        print(f"Cannot reach Polymarket: {e}", file=sys.stderr)
        return 2

    if cfg.dry_run:
        print("DRY RUN -- orders are previewed against the exchange but never submitted.")
        print("Pass --live to trade for real.")
    elif not confirm_live(cfg, bankroll):
        print("Not authorized. Exiting.")
        return 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    gw = GatewayClient()
    traded: Set[str] = set()

    while not _STOP["flag"]:
        try:
            run_cycle(cfg, gw, trader, odds_client, traded)
        except Exception as e:
            print(f"Cycle error: {e}")
            journal(cfg.journal_path, {"type": "cycle_error", "error": str(e)})
        if args.once or _STOP["flag"]:
            break
        time.sleep(cfg.loop_seconds)

    print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
