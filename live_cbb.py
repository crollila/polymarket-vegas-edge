"""
live_cbb.py -- watch live basketball and test the halftime rule. Read-only.

    python live_cbb.py                  # poll live CBB, report halftime signals
    python live_cbb.py --watch          # keep polling until stopped
    python live_cbb.py --sport any      # any live basketball (CBB is Nov-Mar only)
    python live_cbb.py --log            # record signals for forward validation
    python live_cbb.py --show-all       # every live game, signal or not

This places no orders. It exists to answer one question with data: when the
rule says 0.80 and the model says 0.90, what does the market charge, and who
turns out to be right?

Signals are logged through the same store as the pregame scanner, so
`python track.py settle` and `python track.py report` grade them alongside
everything else.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Dict, List, Optional

from vegasanchor.config import Config
from vegasanchor.inplay import (DEFAULT_SIGMA_CBB, InPlayObservation,
                                evaluate_inplay, fraction_remaining, parse_score)
from vegasanchor.oddsapi import (OddsApiClient, OddsApiError, fetch_spreads,
                                 match_spread)
from vegasanchor.polymarket import GatewayClient
from vegasanchor.teams import short_label

# seriesSlug prefixes by sport. CBB runs November to March; out of season the
# scanner will correctly find nothing, which is why --sport any exists for
# testing the plumbing against whatever is actually playing.
SERIES = {
    "cbb": ("cbb",),
    "nba": ("nba",),
    "wnba": ("wnba",),
    "any": ("cbb", "nba", "wnba"),
}

# The Odds API sport keys, for pulling the pregame spread that supplies the
# model's drift term.
SPREAD_KEYS = {
    "cbb": "basketball_ncaab",
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
}


def load_spreads(cfg: Config, sport: str) -> Dict[frozenset, Dict[str, float]]:
    """
    Pregame spreads for the drift term. Empty dict on any failure.

    Without this the model treats every matchup as even, and a heavy underdog
    trailing by 6 registers as a bargain when it is merely losing to form.
    """
    key = SPREAD_KEYS.get(sport)
    if not key:
        return {}
    try:
        client = OddsApiClient(cfg.odds_api_key, cfg.bookmakers, cfg.odds_region)
        return fetch_spreads(client, key)
    except (OddsApiError, ValueError):
        return {}


def collect(gw: GatewayClient, sport: str, cfg: Config,
            allow_midperiod: bool = False,
            spreads: Optional[Dict[frozenset, Dict[str, float]]] = None
            ) -> List[InPlayObservation]:
    """Pull every live basketball game with a usable moneyline book."""
    prefixes = SERIES.get(sport, SERIES["cbb"])
    spreads = spreads or {}
    out: List[InPlayObservation] = []

    for ev in gw.iter_sports_events():
        if not ev.get("live"):
            continue
        series = (ev.get("seriesSlug") or "").lower()
        if not any(series.startswith(p) for p in prefixes):
            continue

        score = parse_score(ev.get("score") or "")
        if not score:
            continue
        frac = fraction_remaining(ev.get("period") or "", sport=series[:4])
        if frac is None:
            # Mid-period: no clock is published, so time remaining is unknown.
            if not allow_midperiod:
                continue
            frac = 0.5
        if frac <= 0.0:
            continue

        for m in ev.get("markets", []) or []:
            if (m.get("marketType") or "").lower() != "moneyline":
                continue
            if m.get("closed") or not m.get("active"):
                continue
            sides = m.get("marketSides") or []
            if len(sides) != 2:
                continue
            lg = next((s for s in sides if s.get("long")), None)
            sh = next((s for s in sides if not s.get("long")), None)
            if not lg or not sh:
                continue
            yes_team = ((lg.get("team") or {}).get("name")) or "?"
            no_team = ((sh.get("team") or {}).get("name")) or "?"

            try:
                bbo = gw.get(f"/v1/markets/{m['slug']}/bbo").get("marketData", {})
                bid = float(bbo["bestBid"]["value"])
                ask = float(bbo["bestAsk"]["value"])
                oi = float(bbo.get("openInterest") or 0.0)
            except Exception:
                continue

            margin = 0.0
            has_line = False
            entry = match_spread(yes_team, no_team, spreads)
            if entry and yes_team in entry:
                margin = entry[yes_team]
                has_line = True

            out.append(InPlayObservation(
                event_slug=ev.get("slug") or "", market_slug=m["slug"],
                yes_team=yes_team, no_team=no_team,
                period=(ev.get("period") or "").strip(),
                yes_score=score[0], no_score=score[1],
                frac_remaining=frac, best_bid=bid, best_ask=ask,
                open_interest=oi, sigma=cfg.inplay_sigma,
                pregame_margin=margin, has_pregame_line=has_line,
            ))
            break
    return out


def run_once(gw: GatewayClient, cfg: Config, args,
             spreads: Optional[Dict[frozenset, Dict[str, float]]] = None) -> int:
    obs = collect(gw, args.sport, cfg, allow_midperiod=args.allow_midperiod,
                  spreads=spreads)
    stamp = time.strftime("%H:%M:%S")
    if not obs:
        print(f"[{stamp}] no live {args.sport.upper()} games at a scoreable moment.")
        if args.sport == "cbb":
            print("        College basketball runs November to March. Use "
                  "--sport any to exercise the harness out of season.")
        return 0

    withline = sum(1 for o in obs if o.has_pregame_line)
    print(f"[{stamp}] {len(obs)} live game(s) with a known clock position "
          f"(sigma={cfg.inplay_sigma:g}, trigger={args.trigger}, "
          f"pregame line on {withline}/{len(obs)})")
    if withline < len(obs):
        print(f"        {len(obs)-withline} game(s) have no spread; those assume "
              f"evenly matched teams, which")
        print(f"        overrates any trailing underdog. Treat their model "
              f"column with suspicion.")

    signals = []
    for o in obs:
        sig = evaluate_inplay(
            o, min_edge=cfg.min_edge, fee_buffer=cfg.fee_buffer,
            max_spread=args.max_spread, require_agreement=args.require_agreement,
            trigger=args.trigger,
        )
        if sig:
            signals.append(sig)
        elif args.show_all:
            print(f"   --  {o.period:<4} {short_label(o.yes_team):>4} "
                  f"{o.yes_score:>3}-{o.no_score:<3} {short_label(o.no_team):<4}"
                  f"  mkt {o.market_mid:.2f}  rule {o.rule_prob():.2f}  "
                  f"model {o.model_prob:.2f}   (no signal)")

    for s in signals:
        print(f"   >>  {s.describe()}")

    if not signals:
        print("        no qualifying signals.")
    elif args.log:
        from vegasanchor.tracking import PredictionStore
        store = PredictionStore()
        n = 0
        for s in signals:
            # One record per market per period, so a long halftime break does
            # not log the same signal a dozen times.
            tag = f"{s.obs.market_slug}#{s.obs.period}"
            if store.already_logged(tag, s.side):
                continue
            store.log_prediction(
                market_slug=tag,
                game=f"{short_label(s.obs.yes_team)} vs {short_label(s.obs.no_team)}",
                side=s.side, bet_on=s.team, cost=s.cost,
                fair=s.rule_fair if args.trigger != "model" else s.model_fair,
                edge=s.rule_edge if args.trigger != "model" else s.model_edge,
                shares=0.0, stake=0.0, kickoff=s.obs.event_slug[-10:],
                books=[f"inplay:{args.trigger}"], source="bot", paper=True,
                teams=[s.obs.yes_team, s.obs.no_team],
            )
            n += 1
        print(f"        logged {n} signal(s) to predictions.jsonl")

    disagree = [s for s in signals if not s.agree]
    if disagree:
        print(f"\n        {len(disagree)} signal(s) where the rule and the model "
              f"disagree.\n        Those are the observations that actually "
              f"discriminate between them.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live in-game halftime-rule harness.")
    ap.add_argument("--sport", choices=list(SERIES), default="cbb")
    ap.add_argument("--trigger", choices=["rule", "model", "both"], default="rule",
                    help="which estimate may fire a signal (default: the bettor's rule)")
    ap.add_argument("--min-edge", type=float, help="required edge (default 0.04)")
    ap.add_argument("--sigma", type=float, help=f"margin SD (default {DEFAULT_SIGMA_CBB:g})")
    ap.add_argument("--max-spread", type=float, default=0.08)
    ap.add_argument("--require-agreement", action="store_true",
                    help="only signal when rule and model both like it")
    ap.add_argument("--allow-midperiod", action="store_true",
                    help="guess t=0.5 mid-period instead of skipping (imprecise)")
    ap.add_argument("--show-all", action="store_true")
    ap.add_argument("--log", action="store_true", help="record signals for grading")
    ap.add_argument("--no-spreads", action="store_true",
                    help="skip the pregame-spread fetch (saves quota, worse model)")
    ap.add_argument("--watch", action="store_true", help="poll continuously")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    cfg = Config.from_env(min_edge=args.min_edge)
    if args.sigma:
        cfg.inplay_sigma = args.sigma

    gw = GatewayClient()
    spreads = {} if args.no_spreads else load_spreads(cfg, args.sport)
    if not args.watch:
        return run_once(gw, cfg, args, spreads)

    print("Watching. Ctrl-C to stop.")
    try:
        while True:
            run_once(gw, cfg, args, spreads)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
