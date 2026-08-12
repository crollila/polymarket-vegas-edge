"""
analyze_history.py -- grade a real Polymarket trading history.

    python analyze_history.py polymarket_trading_history.csv

Expects the columns Polymarket's export provides:
    row_id, action, selection, market, amount_usd, time_shown, cash_flow_usd

Two accounting facts drive everything here, both verified against the ledger
rather than assumed:

  1. A `Won` row's amount is a SHARE COUNT, not a dollar profit. Winning shares
     pay $1 each, so the payout equals the share count -- which is why those
     amounts are always whole numbers, and why cost/payout recovers the average
     entry price.

  2. A `Lost` row is a cost-basis write-off, NOT a cash movement. The money
     already left the account on the `Bought` row. Summing every cash_flow in
     the file therefore double-counts every loss. On this export that is the
     difference between reading the record as -$1,331 and +$643.

Positions still open at export time are excluded from ROI rather than assumed
to be losses, and the cash reconciliation at the end is the number to trust:
deposits and withdrawals cannot be misread.
"""

from __future__ import annotations

import argparse
import collections
import csv
import random
import re
import statistics
import sys
from typing import Dict, List, Optional, Tuple

# Exact full names -- matching on the mascot alone is a trap. "Eastern
# Washington Eagles" and "Eastern Illinois Panthers" are college teams whose
# mascots collide with Philadelphia and Carolina, and a suffix match files them
# under NFL, inflating a small pro sample with college results.
NFL_FULL = {
    "arizona cardinals", "atlanta falcons", "baltimore ravens", "buffalo bills",
    "carolina panthers", "chicago bears", "cincinnati bengals", "cleveland browns",
    "dallas cowboys", "denver broncos", "detroit lions", "green bay packers",
    "houston texans", "indianapolis colts", "jacksonville jaguars", "kansas city chiefs",
    "las vegas raiders", "los angeles chargers", "los angeles rams", "miami dolphins",
    "minnesota vikings", "new england patriots", "new orleans saints", "new york giants",
    "new york jets", "philadelphia eagles", "pittsburgh steelers", "san francisco 49ers",
    "seattle seahawks", "tampa bay buccaneers", "tennessee titans", "washington commanders",
}
# Polymarket lists NBA sides as the bare mascot ("Nets", "Spurs").
NBA_SHORT = {
    "celtics", "nets", "knicks", "76ers", "raptors", "bulls", "cavaliers", "pistons",
    "pacers", "bucks", "hawks", "hornets", "heat", "magic", "wizards", "nuggets",
    "timberwolves", "thunder", "trail blazers", "jazz", "warriors", "clippers",
    "lakers", "suns", "kings", "mavericks", "rockets", "grizzlies", "pelicans", "spurs",
}
NHL_MARKERS = ("bruins", "lightning", "penguins", "senators", "golden knights",
               "canadiens", "maple leafs", "oilers", "canucks", "kraken",
               "avalanche", "blackhawks", "red wings", "sabres")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 &]", " ", (s or "").lower())).strip()


def classify(selection: str, market: str) -> str:
    s, m = norm(selection), norm(market)
    if any(k in m for k in NHL_MARKERS):
        return "NHL"
    if s in NFL_FULL:
        return "NFL"
    if s in NBA_SHORT:
        return "NBA"
    return "CBB"


class Position:
    """All rows for one (market, selection) pair, netted."""

    def __init__(self, market: str, selection: str):
        self.market, self.selection = market, selection
        self.bought = self.sold = self.won = self.lost = 0.0

    @property
    def resolved(self) -> bool:
        return bool(self.won or self.lost)

    @property
    def returned(self) -> float:
        return self.sold + self.won

    @property
    def pnl(self) -> float:
        return self.returned - self.bought

    @property
    def entry_price(self) -> Optional[float]:
        """Average price paid, recoverable only on winners (payout == shares)."""
        if not self.won:
            return None
        px = (self.bought - self.sold) / self.won
        return px if 0.0 < px < 1.0 else None


def load(path: str) -> Tuple[List[Position], Dict[str, float]]:
    positions: Dict[Tuple[str, str], Position] = {}
    cash = collections.defaultdict(float)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            action = (row.get("action") or "").strip()
            try:
                amt = float(row.get("amount_usd") or 0)
                cf = float(row.get("cash_flow_usd") or 0)
            except ValueError:
                continue
            if action in ("Bought", "Sold", "Won", "Lost"):
                key = (row.get("market", ""), row.get("selection", ""))
                p = positions.setdefault(key, Position(*key))
                setattr(p, action.lower(), getattr(p, action.lower()) + abs(amt))
            else:
                cash[action] += cf
    return list(positions.values()), dict(cash)


def bootstrap_ci(sub: List[Position], iterations: int = 10_000,
                 seed: int = 3) -> Optional[Tuple[float, float]]:
    if len(sub) < 3:
        return None
    rng = random.Random(seed)
    n = len(sub)
    out = []
    for _ in range(iterations):
        pick = [sub[rng.randrange(n)] for _ in range(n)]
        cost = sum(x.bought for x in pick)
        if cost > 0:
            out.append(sum(x.pnl for x in pick) / cost)
    if not out:
        return None
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def main() -> int:
    ap = argparse.ArgumentParser(description="Grade a Polymarket trading history export.")
    ap.add_argument("csv")
    ap.add_argument("--balance", type=float, default=None,
                    help="current account balance, to reconcile against cash flows")
    args = ap.parse_args()

    try:
        positions, cash = load(args.csv)
    except OSError as e:
        print(f"Cannot read {args.csv}: {e}", file=sys.stderr)
        return 2

    resolved = [p for p in positions if p.resolved and p.bought > 0]
    unresolved = [p for p in positions if not p.resolved and p.bought > 0]
    orphans = [p for p in positions if p.resolved and p.bought == 0]

    print(f"\n{'='*76}\n  POLYMARKET TRADING HISTORY\n{'='*76}")
    print(f"  positions {len(positions)}   resolved {len(resolved)}   "
          f"still open {len(unresolved)}   buy rows missing {len(orphans)}")

    # ---- by sport ----
    print(f"\n  RESOLVED POSITIONS BY SPORT")
    print(f"  {'SPORT':<6}{'POS':>5}{'WIN%':>7}{'DEPLOYED':>12}{'P&L':>11}{'ROI':>8}   95% CI on ROI")
    print("  " + "-" * 72)
    groups = collections.defaultdict(list)
    for p in resolved:
        groups[classify(p.selection, p.market)].append(p)
    for sp in sorted(groups, key=lambda k: -sum(x.pnl for x in groups[k])):
        sub = groups[sp]
        cost = sum(x.bought for x in sub)
        pnl = sum(x.pnl for x in sub)
        wr = sum(1 for x in sub if x.won) / len(sub) * 100
        ci = bootstrap_ci(sub)
        cis = f"[{ci[0]*100:+7.1f}%, {ci[1]*100:+7.1f}%]" if ci else "        n/a"
        print(f"  {sp:<6}{len(sub):>5}{wr:>6.0f}%{cost:>12,.2f}{pnl:>+11,.2f}"
              f"{pnl/cost*100 if cost else 0:>+7.1f}%   {cis}")
    tc = sum(x.bought for x in resolved)
    tp = sum(x.pnl for x in resolved)
    ci = bootstrap_ci(resolved)
    print("  " + "-" * 72)
    print(f"  {'ALL':<6}{len(resolved):>5}"
          f"{sum(1 for x in resolved if x.won)/len(resolved)*100:>6.0f}%"
          f"{tc:>12,.2f}{tp:>+11,.2f}{tp/tc*100:>+7.1f}%   "
          f"[{ci[0]*100:+7.1f}%, {ci[1]*100:+7.1f}%]" if ci else "")

    # ---- concentration ----
    print(f"\n  CONCENTRATION  (is the result one lucky position?)")
    top = sorted(resolved, key=lambda x: -x.pnl)[:5]
    for p in top:
        share = p.pnl / tp * 100 if tp > 0 else 0
        print(f"    {p.selection[:26]:<28}{p.pnl:>+10,.2f}   {share:>5.1f}% of total profit")
    worst = min(resolved, key=lambda x: x.pnl)
    print(f"    {'(worst) ' + worst.selection[:18]:<28}{worst.pnl:>+10,.2f}")

    # ---- entry prices ----
    pxs = [p.entry_price for p in resolved if p.entry_price]
    if pxs:
        favs = sum(1 for p in pxs if p >= 0.70)
        print(f"\n  ENTRY PRICES  (recoverable on {len(pxs)} winning positions)")
        print(f"    median {statistics.median(pxs):.3f}    mean {statistics.mean(pxs):.3f}")
        print(f"    bought at 0.70 or higher: {favs}/{len(pxs)} ({favs/len(pxs)*100:.0f}%)")

    # ---- open risk ----
    if unresolved:
        oc = sum(p.bought - p.sold for p in unresolved)
        print(f"\n  EXCLUDED FROM THE ABOVE")
        print(f"    {len(unresolved)} positions never settled in this export, "
              f"${oc:,.2f} deployed.")
        print(f"    Counting them as total losses would move overall P&L to "
              f"${tp - oc:+,.2f}.")

    # ---- the number that cannot be misread ----
    deposits = cash.get("Transfer", 0.0) + cash.get("Bonus", 0.0)
    withdrawals = -cash.get("Withdrawal", 0.0)
    print(f"\n  CASH RECONCILIATION")
    print(f"    deposited + bonus   {deposits:>+10,.2f}")
    print(f"    withdrawn           {-withdrawals:>+10,.2f}")
    if args.balance is not None:
        realized = withdrawals + args.balance - deposits
        print(f"    current balance     {args.balance:>+10,.2f}")
        print(f"    ACTUAL ACCOUNT P&L  {realized:>+10,.2f}")
        if deposits > 0:
            print(f"    return on deposits  {realized/deposits*100:>+9.1f}%")
        print(f"\n    This is the honest bottom line: cash in versus cash out.")
        if abs(realized - tp) > 1:
            print(f"    It differs from the ${tp:+,.2f} on resolved positions because "
                  f"open\n    positions and any activity outside this export are "
                  f"captured here.")
    else:
        print(f"    (pass --balance to reconcile against your current balance)")

    print(f"\n{'='*76}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
