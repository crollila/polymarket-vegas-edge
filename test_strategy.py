"""
Offline tests for the parts that decide where your money goes.

    python test_strategy.py

No API key and no network required: the sportsbook feed is faked so the math
can be checked against hand-computed answers. Run this after changing devig,
sizing, or the buy-NO price mapping.
"""

from __future__ import annotations

import sys

from vegasanchor.config import Config
from vegasanchor.devig import (american_to_prob, fair_two_way, hold_pct,
                               multiplicative_devig, power_devig, prob_to_american)
from vegasanchor.edge import (build_order_payload, evaluate, kelly_fraction,
                              round_to_tick, scan, size_recommendation)
from vegasanchor.oddsapi import BookLine, GameOdds, OddsApiClient, index_by_teams
from vegasanchor.polymarket import MoneylineMarket
from vegasanchor.teams import canonical_team

PASS, FAIL = 0, 0


def check(name: str, got, want, tol: float = 1e-6) -> None:
    global PASS, FAIL
    ok = (abs(got - want) <= tol) if isinstance(want, float) else (got == want)
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def make_market(yes_team, no_team, bid, ask, oi=10_000.0, slug="test-market") -> MoneylineMarket:
    m = MoneylineMarket(
        event_slug="ev", market_slug=slug, question="q",
        yes_team=yes_team, no_team=no_team,
        kickoff="2026-09-14T17:00:00Z", live=False, tick_size=0.01,
    )
    m.best_bid, m.best_ask, m.open_interest = bid, ask, oi
    return m


def make_game(home, away, home_odds, away_odds, method="power") -> GameOdds:
    g = GameOdds(event_id="e1", sport_key="americanfootball_nfl",
                 commence_time="2026-09-14T17:00:00Z", home_team=home, away_team=away)
    g.lines = [BookLine(book="fanduel",
                        american={home: home_odds, away: away_odds},
                        hold=hold_pct(home_odds, away_odds))]
    fh, fa = fair_two_way(home_odds, away_odds, method=method)
    g.fair = {home: fh, away: fa}
    g.books_used = ["fanduel"]
    return g


print("\n== odds conversion ==")
check("-110 -> 0.5238", american_to_prob(-110), 0.5238095, 1e-6)
check("+150 -> 0.40", american_to_prob(150), 0.40, 1e-9)
check("-110/-110 hold ~4.76%", round(hold_pct(-110, -110), 2), 4.76, 1e-9)
check("devig sums to 1", sum(multiplicative_devig([0.5238, 0.5238])), 1.0, 1e-9)
check("power devig sums to 1", sum(power_devig([0.9231, 0.1304])), 1.0, 1e-9)
check("prob->american 0.5238 ~ -110", prob_to_american(0.5238095), -110)
check("even market devigs to 0.5", fair_two_way(-110, -110)[0], 0.5, 1e-6)

print("\n== power vs multiplicative on a heavy favorite ==")
# -1200 / +750 : power devig should hold the favorite HIGHER than multiplicative,
# because it strips proportionally more vig from the longshot.
p_fav, _ = fair_two_way(-1200, 750, "power")
m_fav, _ = fair_two_way(-1200, 750, "multiplicative")
check("power > multiplicative on favorite", p_fav > m_fav, True)
check("gap is material (>1.5 pts)", (p_fav - m_fav) > 0.015, True)

print("\n== kelly ==")
check("no edge -> 0 stake", kelly_fraction(0.50, 0.50), 0.0)
check("negative edge -> 0 stake", kelly_fraction(0.40, 0.50), 0.0)
# fair 0.60 at cost 0.50 -> (0.60-0.50)/(1-0.50) = 0.20
check("f* = (p-c)/(1-c)", kelly_fraction(0.60, 0.50), 0.20, 1e-9)
check("cost 0 is rejected", kelly_fraction(0.9, 0.0), 0.0)
check("cost 1 is rejected", kelly_fraction(0.9, 1.0), 0.0)

print("\n== tick rounding ==")
check("nearest", round_to_tick(0.5449, 0.01), 0.54, 1e-9)
check("up", round_to_tick(0.5401, 0.01, "up"), 0.55, 1e-9)
check("down", round_to_tick(0.5499, 0.01, "down"), 0.54, 1e-9)
check("exact stays put (up)", round_to_tick(0.55, 0.01, "up"), 0.55, 1e-9)
check("exact stays put (down)", round_to_tick(0.55, 0.01, "down"), 0.55, 1e-9)
check("clamped below 1", round_to_tick(1.5, 0.01) < 1.0, True)

print("\n== buy-NO price mapping (the bug that broke v1) ==")
mkt = make_market("Detroit Lions", "Cincinnati Bengals", bid=0.27, ask=0.28)
check("YES taker cost == ask", mkt.yes_cost_taker, 0.28, 1e-9)
check("NO taker cost == 1 - bid", mkt.no_cost_taker, 0.73, 1e-9)
check("costs exceed 1 by the spread", mkt.yes_cost_taker + mkt.no_cost_taker, 1.01, 1e-9)

print("\n== side selection ==")
cfg = Config(min_edge=0.04, fee_buffer=0.0, min_open_interest=0.0, max_spread=0.06)

# Book says Cincinnati is a big favorite; Polymarket prices Detroit YES at 0.28,
# so NO (Cincinnati) costs 0.73 while its fair value is ~0.83. Buy NO.
game = make_game(home="Cincinnati Bengals", away="Detroit Lions",
                 home_odds=-500, away_odds=380)
rec = evaluate(mkt, game, cfg)
check("found a bet", rec is not None, True)
check("picked the NO side", rec.side, "NO")
check("betting on Cincinnati", rec.team, "Cincinnati Bengals")
check("cost is 1 - bid", rec.cost, 0.73, 1e-9)
check("intent is BUY_SHORT", rec.intent, "ORDER_INTENT_BUY_SHORT")
check("limit price is YES-basis bid", rec.limit_yes_price, 0.27, 1e-9)
check("edge = fair - cost", rec.edge, game.fair["Cincinnati Bengals"] - 0.73, 1e-9)

# Flip it: book likes Detroit, YES is cheap at 0.28.
game2 = make_game(home="Cincinnati Bengals", away="Detroit Lions",
                  home_odds=250, away_odds=-320)
rec2 = evaluate(mkt, game2, cfg)
check("picked the YES side", rec2.side, "YES")
check("betting on Detroit", rec2.team, "Detroit Lions")
check("cost is the ask", rec2.cost, 0.28, 1e-9)
check("intent is BUY_LONG", rec2.intent, "ORDER_INTENT_BUY_LONG")
check("limit price is the ask", rec2.limit_yes_price, 0.28, 1e-9)

print("\n== filters reject bad markets ==")
fair_mkt = make_market("Detroit Lions", "Cincinnati Bengals", bid=0.16, ask=0.17)
agree = make_game("Cincinnati Bengals", "Detroit Lions", -500, 380)
check("no edge when PM agrees with the book", evaluate(fair_mkt, agree, cfg), None)

wide = make_market("Detroit Lions", "Cincinnati Bengals", bid=0.20, ask=0.40)
check("wide spread rejected", evaluate(wide, game, cfg), None)

thin = make_market("Detroit Lions", "Cincinnati Bengals", bid=0.27, ask=0.28, oi=10.0)
check("thin book rejected", evaluate(thin, game, Config(min_open_interest=500.0)), None)

wrong = make_market("Green Bay Packers", "Chicago Bears", bid=0.27, ask=0.28)
check("mismatched teams rejected", evaluate(wrong, game, cfg), None)

crossed = make_market("Detroit Lions", "Cincinnati Bengals", bid=0.50, ask=0.40)
check("crossed book rejected", evaluate(crossed, game, cfg), None)

print("\n== fee buffer ==")
tight = Config(min_edge=0.04, fee_buffer=0.05, min_open_interest=0.0)
before = evaluate(mkt, game, cfg)
after = evaluate(mkt, game, tight)
check("buffer shrinks the edge", after is None or after.edge < before.edge, True)

print("\n== sizing ==")
cfg_size = Config(min_edge=0.04, fee_buffer=0.0, min_open_interest=0.0,
                  kelly_fraction=0.25, max_pct_bankroll_per_trade=0.10, min_order_usd=5.0)
sized = size_recommendation(evaluate(mkt, game, cfg_size), 1000.0, cfg_size)
check("stake respects the 10% cap", sized.stake_usd <= 100.0 + 0.73, True)
check("shares are whole", sized.shares == int(sized.shares), True)
check("stake ~= shares * cost", sized.stake_usd, round(sized.shares * sized.cost, 2), 0.02)
check("positive EV", sized.expected_value_usd > 0, True)

tiny = size_recommendation(evaluate(mkt, game, cfg_size), 20.31, cfg_size)
check("tiny bankroll -> below exchange minimum -> 0 shares", tiny.shares, 0)
check("tiny bankroll -> no stake", tiny.stake_usd, 0.0, 1e-9)

print("\n== order payload ==")
payload = build_order_payload(sized, cfg_size)
check("slug carried through", payload["marketSlug"], "test-market")
check("limit type", payload["type"], "ORDER_TYPE_LIMIT")
check("price is a 3dp string", payload["price"]["value"], "0.270")
check("BUY_SHORT for a NO bet", payload["intent"], "ORDER_INTENT_BUY_SHORT")
check("quantity is an int", isinstance(payload["quantity"], int), True)
try:
    build_order_payload(tiny, cfg_size)
    check("zero-quantity order refused", False, True)
except ValueError:
    check("zero-quantity order refused", True, True)

print("\n== end-to-end scan ==")
markets = [
    make_market("Detroit Lions", "Cincinnati Bengals", 0.27, 0.28, slug="m1"),
    make_market("Green Bay Packers", "Pittsburgh Steelers", 0.54, 0.55, slug="m2"),
    make_market("Los Angeles Chargers", "Houston Texans", 0.46, 0.47, slug="m3"),
]
games = [
    make_game("Cincinnati Bengals", "Detroit Lions", -500, 380),   # big edge on NO
    make_game("Pittsburgh Steelers", "Green Bay Packers", -105, -115),  # agrees, no bet
    make_game("Houston Texans", "Los Angeles Chargers", 120, -140),     # edge on YES
]
recs = scan(markets, index_by_teams(games), cfg, 1000.0)
check("only the mispriced games surface", len(recs), 2)
check("ranked by edge descending", recs[0].edge >= recs[1].edge, True)
check("no bet on the agreeing market", all(r.market.market_slug != "m2" for r in recs), True)

print("\n== odds api parsing ==")
raw_event = {
    "id": "abc", "commence_time": "2026-09-14T17:00:00Z",
    "home_team": "Cincinnati Bengals", "away_team": "Detroit Lions",
    "bookmakers": [{"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Cincinnati Bengals", "price": -500},
        {"name": "Detroit Lions", "price": 380}]}]}],
}
parsed = OddsApiClient._parse_event(raw_event, "americanfootball_nfl", "power")
check("event parsed", parsed is not None, True)
check("fair probs sum to 1", sum(parsed.fair.values()), 1.0, 1e-6)
check("favorite is the Bengals", parsed.fair["Cincinnati Bengals"] > 0.5, True)
check("book price recovered", parsed.american_for("Detroit Lions"), 380)

one_sided = {**raw_event, "bookmakers": [{"key": "fanduel", "markets": [
    {"key": "h2h", "outcomes": [{"name": "Cincinnati Bengals", "price": -500}]}]}]}
check("one-sided quote dropped", OddsApiClient._parse_event(one_sided, "k", "power"), None)

non_nfl = {**raw_event, "home_team": "Boston Celtics", "away_team": "Miami Heat"}
check("non-NFL event dropped", OddsApiClient._parse_event(non_nfl, "k", "power"), None)

print("\n== team matching ==")
check("ambiguous city refused", canonical_team("Los Angeles"), None)
check("polymarket safeName resolved", canonical_team("Los Angeles C"), "Los Angeles Chargers")
check("abbreviation resolved", canonical_team("gb"), "Green Bay Packers")
check("stale name resolved", canonical_team("Washington Football Team"), "Washington Commanders")

print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed\n{'=' * 40}")
sys.exit(1 if FAIL else 0)
