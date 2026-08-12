"""
The actual strategy: compare a devigged sportsbook price to what Polymarket
charges, and size the difference.

Execution prices matter as much as the signal. On a Polymarket US moneyline
there is one YES contract and a bid/ask around it:

    BUY YES  (taker) -> pay best_ask         limit price (YES basis) = best_ask
    BUY NO   (taker) -> pay 1 - best_bid     limit price (YES basis) = best_bid
    BUY YES  (maker) -> pay best_bid         rests at the bid
    BUY NO   (maker) -> pay 1 - best_ask     rests at the ask

Comparing your fair value against the midpoint instead of against the price
you actually pay is the single easiest way to talk yourself into a losing bet:
on a 2-cent-wide market the mid flatters every trade by a full cent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .config import Config
from .oddsapi import GameOdds
from .polymarket import MoneylineMarket
from .teams import short_label


@dataclass
class Recommendation:
    market: MoneylineMarket
    game: GameOdds
    side: str                 # "YES" or "NO"
    team: str                 # the team you are betting ON (canonical)
    cost: float               # price per share you pay, 0..1
    fair: float               # devigged sportsbook probability for `team`
    edge: float               # fair - cost, after the fee buffer
    limit_yes_price: float    # what goes in the order payload (always YES basis)
    intent: str               # ORDER_INTENT_BUY_LONG / ORDER_INTENT_BUY_SHORT
    stake_usd: float = 0.0
    shares: int = 0
    book_american: Optional[int] = None

    @property
    def edge_pct(self) -> float:
        return self.edge * 100.0

    @property
    def action(self) -> str:
        """Plain-English instruction."""
        return f"BUY {self.side} on {short_label(self.team)}"

    @property
    def expected_value_usd(self) -> float:
        """EV of the stake if the devigged book price is the true probability."""
        if self.cost <= 0:
            return 0.0
        return self.shares * (self.fair - self.cost)

    def describe(self) -> str:
        return (
            f"{self.market.label():>13}  {self.action:<18} "
            f"@ {self.cost:.2f}  fair {self.fair:.2f}  "
            f"edge {self.edge_pct:+.1f}%  ${self.stake_usd:,.2f} ({self.shares} sh)"
        )


def kelly_fraction(fair: float, cost: float) -> float:
    """
    Kelly stake for a binary contract that pays $1 and costs `cost`.

        f* = (p - c) / (1 - c)

    Zero or negative when you have no edge.
    """
    if not (0.0 < cost < 1.0):
        return 0.0
    f = (fair - cost) / (1.0 - cost)
    return max(0.0, min(1.0, f))


def round_to_tick(price: float, tick: float, direction: str = "nearest") -> float:
    """Snap a limit price to the exchange tick grid."""
    if tick <= 0:
        tick = 0.01
    steps = price / tick
    if direction == "up":
        import math
        steps = math.ceil(steps - 1e-9)
    elif direction == "down":
        import math
        steps = math.floor(steps + 1e-9)
    else:
        steps = round(steps)
    out = steps * tick
    return max(tick, min(1.0 - tick, round(out, 6)))


def _hours_until(iso_ts: str) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


def evaluate(market: MoneylineMarket, game: GameOdds, cfg: Config) -> Optional[Recommendation]:
    """
    Score one Polymarket market against one sportsbook game.

    Returns the better of the two sides if it clears every filter, else None.
    """
    if market.teams_mismatch(game):
        return None
    if not (0.0 < market.best_bid <= market.best_ask < 1.0):
        return None
    if market.spread > cfg.max_spread:
        return None
    if market.open_interest < cfg.min_open_interest:
        return None

    fair_yes = game.fair.get(market.yes_team)
    fair_no = game.fair.get(market.no_team)
    if fair_yes is None or fair_no is None:
        return None

    candidates: List[Recommendation] = []

    # --- YES side ---
    yes_cost = market.best_bid if cfg.maker_only else market.best_ask
    yes_edge = fair_yes - yes_cost - cfg.fee_buffer
    if cfg.min_price <= yes_cost <= cfg.max_price:
        candidates.append(Recommendation(
            market=market, game=game, side="YES", team=market.yes_team,
            cost=yes_cost, fair=fair_yes, edge=yes_edge,
            limit_yes_price=round_to_tick(yes_cost, market.tick_size,
                                          "down" if cfg.maker_only else "up"),
            intent="ORDER_INTENT_BUY_LONG",
            book_american=game.american_for(market.yes_team),
        ))

    # --- NO side ---
    # Buying NO means selling YES: cost is 1 - (the YES price you sell at).
    no_yes_basis = market.best_ask if cfg.maker_only else market.best_bid
    no_cost = 1.0 - no_yes_basis
    no_edge = fair_no - no_cost - cfg.fee_buffer
    if cfg.min_price <= no_cost <= cfg.max_price:
        candidates.append(Recommendation(
            market=market, game=game, side="NO", team=market.no_team,
            cost=no_cost, fair=fair_no, edge=no_edge,
            limit_yes_price=round_to_tick(no_yes_basis, market.tick_size,
                                          "up" if cfg.maker_only else "down"),
            intent="ORDER_INTENT_BUY_SHORT",
            book_american=game.american_for(market.no_team),
        ))

    if not candidates:
        return None
    best = max(candidates, key=lambda c: c.edge)
    if best.edge < cfg.min_edge:
        return None
    return best


def size_recommendation(rec: Recommendation, bankroll: float, cfg: Config) -> Recommendation:
    """Attach a dollar stake and share count using fractional Kelly."""
    f = kelly_fraction(rec.fair, rec.cost) * cfg.kelly_fraction
    stake = min(bankroll * f, bankroll * cfg.max_pct_bankroll_per_trade)

    shares = int(stake / rec.cost) if rec.cost > 0 else 0
    # An order below the exchange minimum will just be rejected; report 0 so the
    # caller can say "edge found, bankroll too small" instead of failing at submit.
    if shares * rec.cost < cfg.min_order_usd:
        shares = 0
        stake = 0.0
    else:
        stake = shares * rec.cost

    rec.shares = shares
    rec.stake_usd = round(stake, 2)
    return rec


def scan(markets: Sequence[MoneylineMarket], games_by_teams: Dict[frozenset, GameOdds],
         cfg: Config, bankroll: float) -> List[Recommendation]:
    """Full pass: match every market to its game, score it, size it, rank it."""
    recs: List[Recommendation] = []
    for m in markets:
        game = games_by_teams.get(frozenset((m.yes_team, m.no_team)))
        if not game:
            continue
        rec = evaluate(m, game, cfg)
        if rec:
            recs.append(size_recommendation(rec, bankroll, cfg))
    recs.sort(key=lambda r: r.edge, reverse=True)
    return recs


def build_order_payload(rec: Recommendation, cfg: Config) -> Dict[str, object]:
    """
    Translate a recommendation into a Polymarket US order.

    `price.value` is ALWAYS the YES price, for both intents -- buying NO at 0.73
    is expressed as BUY_SHORT with price 0.27.
    """
    px = rec.limit_yes_price
    if not (0.001 <= px <= 0.999):
        raise ValueError(f"limit price out of bounds: {px}")
    if rec.shares <= 0:
        raise ValueError("refusing to build a zero-quantity order")
    return {
        "marketSlug": rec.market.market_slug,
        "intent": rec.intent,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": f"{px:.3f}", "currency": "USD"},
        "quantity": int(rec.shares),
        "tif": cfg.tif,
        "participateDontInitiate": bool(cfg.maker_only),
        "manualOrderIndicator": cfg.manual_indicator,
    }
