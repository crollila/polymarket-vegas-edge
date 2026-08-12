"""
Offline tests for the live in-game harness.

    python test_inplay.py

No network. Covers score orientation, the period-to-clock mapping (including
the cases that must refuse to answer), the win-probability model, the step
rule, spread matching, and signal generation.
"""

from __future__ import annotations

import sys

from vegasanchor.inplay import (DEFAULT_STEP_RULE, InPlayObservation,
                                brownian_win_prob, calibrate_sigma,
                                evaluate_inplay, fraction_remaining,
                                parse_score, step_rule_prob)
from vegasanchor.oddsapi import _names_match, match_spread

PASS = FAIL = 0


def check(name, got, want, tol=1e-9):
    global PASS, FAIL
    ok = abs(got - want) <= tol if isinstance(want, float) and isinstance(got, (int, float)) else got == want
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def obs(yes=50, no=40, period="HT", frac=0.5, bid=0.70, ask=0.72,
        margin=0.0, line=False, sigma=11.0):
    return InPlayObservation(
        event_slug="e", market_slug="m", yes_team="Duke", no_team="Kansas",
        period=period, yes_score=yes, no_score=no, frac_remaining=frac,
        best_bid=bid, best_ask=ask, sigma=sigma,
        pregame_margin=margin, has_pregame_line=line)


print("\n== score parsing (YES side first, verified on 10 live games) ==")
check("normal", parse_score("50-56"), (50, 56))
check("zeros", parse_score("0-0"), (0, 0))
check("three digits", parse_score("132-206"), (132, 206))
check("whitespace tolerated", parse_score(" 7 - 3 "), (7, 3))
check("garbage refused", parse_score("bad"), None)
check("empty refused", parse_score(""), None)
check("None refused", parse_score(None), None)

print("\n== period -> fraction remaining ==")
check("halftime is exact", fraction_remaining("HT"), 0.5)
check("halftime lowercase", fraction_remaining("ht"), 0.5)
check("end of first half", fraction_remaining("End 1H"), 0.5)
check("final", fraction_remaining("FT"), 0.0)
check("not started", fraction_remaining("NS"), 1.0)
check("end Q1 (quarters)", fraction_remaining("End Q1", "nba"), 0.75)
check("end Q3 (quarters)", fraction_remaining("End Q3", "nba"), 0.25)
# The whole point: mid-period has no clock, so it must refuse rather than guess.
check("mid-quarter refuses", fraction_remaining("Q3", "nba"), None)
check("mid-half refuses", fraction_remaining("H2"), None)
check("soccer clock refuses", fraction_remaining("71'"), None)
check("empty refuses", fraction_remaining(""), None)

print("\n== brownian win probability ==")
check("tied at half is a coin flip", brownian_win_prob(0, 0.5), 0.5, 1e-12)
check("symmetry", brownian_win_prob(7, 0.5) + brownian_win_prob(-7, 0.5), 1.0, 1e-12)
check("game over, leading", brownian_win_prob(10, 0.0), 1.0)
check("game over, trailing", brownian_win_prob(-10, 0.0), 0.0)
check("game over, tied", brownian_win_prob(0, 0.0), 0.5)
check("same lead is safer with less time left",
      brownian_win_prob(10, 0.25) > brownian_win_prob(10, 0.5), True)
check("bigger lead is better", brownian_win_prob(15, 0.5) > brownian_win_prob(10, 0.5), True)
check("higher sigma is less certain",
      brownian_win_prob(10, 0.5, 0, 20) < brownian_win_prob(10, 0.5, 0, 11), True)
check("frac > 1 is clamped", brownian_win_prob(0, 5.0), 0.5, 1e-12)
# The drift term is what makes an underdog's deficit unremarkable.
fav = brownian_win_prob(-7, 0.5, pregame_margin=+10)
dog = brownian_win_prob(-7, 0.5, pregame_margin=-10)
check("same deficit, favourite still likelier", fav > dog, True)
check("pregame margin materially moves it", (fav - dog) > 0.30, True)

print("\n== the step rule ==")
check("stated anchor: +10 -> 0.80", step_rule_prob(10), 0.80)
check("above anchor uses higher tier", step_rule_prob(16), 0.90)
check("tied", step_rule_prob(0), 0.50)
check("symmetric", step_rule_prob(-10), 0.20, 1e-12)
check("just under a threshold", step_rule_prob(9), 0.68)
check("custom table honoured", step_rule_prob(10, [(10, 0.85), (0, 0.5)]), 0.85)

print("\n== rule vs model disagree at the trigger point ==")
# This gap is the reason the harness exists.
r, m = step_rule_prob(10), brownian_win_prob(10, 0.5)
check("model is more confident than the rule at +10", m > r, True)
check("gap is material (>5 pts)", (m - r) > 0.05, True)

print("\n== observation arithmetic ==")
o = obs(yes=50, no=40, bid=0.70, ask=0.72)
check("lead", o.yes_lead, 10)
check("mid", o.market_mid, 0.71)
check("YES cost is the ask", o.yes_cost, 0.72)
check("NO cost is 1 - bid", o.no_cost, 0.30)

print("\n== signal generation ==")
# Rule says 0.80, market asks 0.72 -> an 8-point edge, clears a 4% bar.
s = evaluate_inplay(obs(bid=0.70, ask=0.72), min_edge=0.04, fee_buffer=0.0)
check("fires on the rule", s is not None, True)
check("buys YES", s.side, "YES")
check("backs the leader", s.team, "Duke")
check("rule fair is 0.80", s.rule_fair, 0.80)
check("edge is fair - cost", s.rule_edge, 0.08, 1e-9)
check("model also likes it -> agree", s.agree, True)

# Market already above the rule's number: no rule signal.
check("no signal when market exceeds the rule",
      evaluate_inplay(obs(bid=0.84, ask=0.86), min_edge=0.04, fee_buffer=0.0), None)
# ...but the model still likes it, which is the discriminating case.
sm = evaluate_inplay(obs(bid=0.84, ask=0.86), min_edge=0.04, fee_buffer=0.0, trigger="model")
check("model fires where the rule does not", sm is not None, True)
check("that signal is flagged as disagreement", sm.agree, False)
check("'both' trigger refuses it",
      evaluate_inplay(obs(bid=0.84, ask=0.86), min_edge=0.04, fee_buffer=0.0,
                      trigger="both"), None)

print("\n== filters ==")
check("wide spread rejected",
      evaluate_inplay(obs(bid=0.50, ask=0.95), min_edge=0.04, max_spread=0.08), None)
check("crossed book rejected",
      evaluate_inplay(obs(bid=0.80, ask=0.70), min_edge=0.04), None)
check("fee buffer can kill a marginal edge",
      evaluate_inplay(obs(bid=0.70, ask=0.77), min_edge=0.04, fee_buffer=0.05), None)
check("require_agreement filters disagreements",
      evaluate_inplay(obs(bid=0.84, ask=0.86), min_edge=0.04, fee_buffer=0.0,
                      trigger="model", require_agreement=True), None)

print("\n== a trailing underdog is not a bargain ==")
# Down 7 at half. Assuming the teams were even (no line) rates the trailing
# side higher than accounting for the fact that it was expected to lose.
# Whether that lands nearer the market depends on the actual spread, so the
# testable claim is the direction and its monotonicity, not the distance.
naive = obs(yes=54, no=61, bid=0.17, ask=0.19, margin=0.0)
withline = obs(yes=54, no=61, bid=0.17, ask=0.19, margin=-9.0, line=True)
check("no-line model overrates the trailing underdog",
      naive.model_prob > withline.model_prob, True)
check("and by a lot (>8 pts)", (naive.model_prob - withline.model_prob) > 0.08, True)
probs = [obs(yes=54, no=61, margin=mu).model_prob for mu in (+10, +5, 0, -5, -10)]
check("monotonically decreasing in a worsening pregame margin",
      all(a > b for a, b in zip(probs, probs[1:])), True)
check("a trailing pregame favourite still beats a trailing pregame dog",
      probs[0] > probs[-1], True)
# The flag exists so the caller knows when the drift term is an assumption.
check("no-line observation is flagged", naive.has_pregame_line, False)
check("lined observation is flagged", withline.has_pregame_line, True)

print("\n== spread matching across feeds ==")
check("city prefix matches", _names_match("Washington", "Washington Mystics"), True)
check("exact matches", _names_match("Duke Blue Devils", "duke blue devils"), True)
check("different teams do not", _names_match("Washington", "Las Vegas Aces"), False)
check("mascot alone does not", _names_match("Sparks", "Los Angeles Sparks"), False)
uniq = {frozenset(("Washington Mystics", "Las Vegas Aces")):
        {"Washington Mystics": 3.5, "Las Vegas Aces": -3.5}}
check("unique pair matches and re-keys",
      match_spread("Washington", "Las Vegas", uniq), {"Washington": 3.5, "Las Vegas": -3.5})
check("unknown pair returns None", match_spread("Denver", "Utah", uniq), None)
amb = {frozenset(("Los Angeles Lakers", "Boston A")): {"Los Angeles Lakers": -4.0, "Boston A": 4.0},
       frozenset(("Los Angeles Clippers", "Boston B")): {"Los Angeles Clippers": -2.0, "Boston B": 2.0}}
check("ambiguous city refuses rather than guesses",
      match_spread("Los Angeles", "Boston", amb), None)

print("\n== sigma calibration ==")
check("too few observations -> None", calibrate_sigma([(10, 0.5, True)] * 5), None)
# Generate outcomes from a known sigma; the fit should recover roughly that.
import random
rng = random.Random(5)
true_sigma = 11.0
data = []
for _ in range(3000):
    lead = rng.uniform(-25, 25)
    t = rng.choice([0.5, 0.25, 0.75])
    p = brownian_win_prob(lead, t, 0.0, true_sigma)
    data.append((lead, t, rng.random() < p))
fit = calibrate_sigma(data)
check("recovers sigma within 25%", abs(fit - true_sigma) / true_sigma < 0.25, True)
print(f"       true sigma {true_sigma}, fitted {fit:.2f}")

print(f"\n{'='*44}\n{PASS} passed, {FAIL} failed\n{'='*44}")
sys.exit(1 if FAIL else 0)
