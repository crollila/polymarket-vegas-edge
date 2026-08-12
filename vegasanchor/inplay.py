"""
Live in-game win probability for basketball, and the harness for testing a
halftime rule against the market.

The rule under test: "a team up 10+ at halftime wins about 80% of the time, so
buy it whenever the market prices it below 0.80."

Three probabilities are compared at every observation:

  MARKET   what Polymarket charges right now
  RULE     the step table the bettor actually used
  MODEL    a Brownian-motion estimate (Stern 1994)

The rule and the model disagree at exactly the rule's trigger point -- a
10-point halftime lead is 0.80 by the rule and ~0.90 by the model -- so the
market's price adjudicates between them. That disagreement is the reason this
harness logs rather than assumes.

TIMING CONSTRAINT: the gateway publishes `period` but no game clock. Halftime
is therefore the only moment where time remaining is known exactly (t = 0.5),
which happens to be precisely when the rule applies. Mid-half observations are
skipped by default rather than guessed at.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Score strings are "<long side>-<short side>", i.e. YES team first. Verified
# against 10 concurrent live games: in every one, the sign of the YES lead
# implied by this ordering agreed with the side the market favoured, including
# a -74 blowout priced at 0.07 and a +5 lead at 0.95.
SCORE_RE = re.compile(r"^\s*(-?\d+)\s*-\s*(-?\d+)\s*$")

# Standard deviation of a full-game final margin around its pregame
# expectation. ~11 points is the usual figure quoted for college basketball.
# It is exposed as a parameter because it is the one number here worth fitting
# from your own logged outcomes rather than taking on faith.
DEFAULT_SIGMA_CBB = 11.0

# The bettor's stated rule. Only the +10 -> 0.80 entry came from the actual
# strategy; the rest are placeholders to be replaced with the real thresholds
# (or fitted from logged data). Kept explicit so it is obvious what is assumed.
DEFAULT_STEP_RULE: List[Tuple[int, float]] = [
    (20, 0.95),
    (15, 0.90),
    (10, 0.80),   # <- the stated anchor
    (5,  0.68),
    (1,  0.57),
    (0,  0.50),
]


def parse_score(score: str) -> Optional[Tuple[int, int]]:
    """'50-56' -> (50, 56), YES side first. None if unparseable."""
    if not score:
        return None
    m = SCORE_RE.match(str(score))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def fraction_remaining(period: str, sport: str = "cbb") -> Optional[float]:
    """
    Fraction of regulation left, from the period label alone.

    Returns None whenever the label does not pin down the time -- mid-half and
    mid-quarter states are genuinely unknown without a clock, and guessing
    "about half the half is gone" injects error straight into the win
    probability at the moment it matters most.
    """
    p = (period or "").strip().upper().replace(".", "")
    if not p:
        return None

    if p in ("HT", "HALF", "HALFTIME", "END 1H", "END H1"):
        return 0.5
    if p in ("FT", "FINAL", "F", "POST", "VFT", "ENDED"):
        return 0.0
    if p in ("NS", "PRE", "SCHEDULED"):
        return 1.0

    # End-of-quarter markers are exact for quarter-based basketball.
    if sport in ("nba", "wnba"):
        end_q = {"END Q1": 0.75, "END Q2": 0.5, "END Q3": 0.25, "END Q4": 0.0}
        if p in end_q:
            return end_q[p]

    # Anything else (Q3, H2, "71'") is mid-period: time unknown.
    return None


def brownian_win_prob(lead: float, frac_remaining: float,
                      pregame_margin: float = 0.0,
                      sigma: float = DEFAULT_SIGMA_CBB) -> float:
    """
    Stern's Brownian-motion in-game win probability.

        P(win) = Phi( (L + mu * t) / (sigma * sqrt(t)) )

    L is the current lead, t the fraction of the game remaining, mu the
    pregame expected margin over a full game (0 if you have no line), and
    sigma the SD of the final margin.

    Intuition check: at halftime (t = 0.5) with a 10-point lead and no pregame
    edge, this returns ~0.90 -- meaningfully more confident than the 0.80 the
    step rule assumes.
    """
    if frac_remaining <= 0:
        return 1.0 if lead > 0 else (0.0 if lead < 0 else 0.5)
    if frac_remaining > 1:
        frac_remaining = 1.0
    denom = sigma * math.sqrt(frac_remaining)
    if denom <= 0:
        return 1.0 if lead > 0 else 0.0
    z = (lead + pregame_margin * frac_remaining) / denom
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def step_rule_prob(lead: float,
                   table: Optional[List[Tuple[int, float]]] = None) -> float:
    """
    The bettor's own heuristic: a step function on the halftime lead.

    Symmetric -- trailing by 10 is 1 - P(leading by 10).
    """
    tbl = table if table is not None else DEFAULT_STEP_RULE
    mag = abs(lead)
    prob = 0.5
    for threshold, p in sorted(tbl, key=lambda x: -x[0]):
        if mag >= threshold:
            prob = p
            break
    return prob if lead >= 0 else 1.0 - prob


@dataclass
class InPlayObservation:
    """One live game at one moment, with all three probability views."""
    event_slug: str
    market_slug: str
    yes_team: str
    no_team: str
    period: str
    yes_score: int
    no_score: int
    frac_remaining: float
    best_bid: float
    best_ask: float
    open_interest: float = 0.0
    sigma: float = DEFAULT_SIGMA_CBB
    pregame_margin: float = 0.0     # expected YES margin from the pregame spread
    has_pregame_line: bool = False  # False means pregame_margin is an assumption

    @property
    def yes_lead(self) -> int:
        return self.yes_score - self.no_score

    @property
    def market_mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def model_prob(self) -> float:
        return brownian_win_prob(self.yes_lead, self.frac_remaining,
                                 self.pregame_margin, self.sigma)

    def rule_prob(self, table: Optional[List[Tuple[int, float]]] = None) -> float:
        return step_rule_prob(self.yes_lead, table)

    @property
    def yes_cost(self) -> float:
        """Cost per share to buy YES now (lift the offer)."""
        return self.best_ask

    @property
    def no_cost(self) -> float:
        """Cost per share to buy NO now (hit the bid)."""
        return 1.0 - self.best_bid


@dataclass
class InPlaySignal:
    obs: InPlayObservation
    side: str                # YES or NO
    team: str                # team backed
    cost: float
    rule_fair: float         # the step rule's probability for `team`
    model_fair: float        # the Brownian model's probability for `team`
    source: str              # which estimate triggered the signal

    @property
    def rule_edge(self) -> float:
        return self.rule_fair - self.cost

    @property
    def model_edge(self) -> float:
        return self.model_fair - self.cost

    @property
    def agree(self) -> bool:
        """Both estimates call it a bet. Disagreement is the interesting case."""
        return self.rule_edge > 0 and self.model_edge > 0

    def describe(self) -> str:
        from .teams import short_label
        flag = "" if self.agree else "   [rule/model disagree]"
        return (f"{self.obs.period:<4} {self.obs.yes_team[:14]:<15}"
                f"{self.obs.yes_score:>3}-{self.obs.no_score:<3}"
                f"{self.obs.no_team[:14]:<15} BUY {self.side:<3} "
                f"@ {self.cost:.2f}  rule {self.rule_fair:.2f} "
                f"({self.rule_edge*100:+.0f})  model {self.model_fair:.2f} "
                f"({self.model_edge*100:+.0f}){flag}")


def evaluate_inplay(obs: InPlayObservation, min_edge: float = 0.04,
                    fee_buffer: float = 0.005,
                    max_spread: float = 0.08,
                    require_agreement: bool = False,
                    trigger: str = "rule",
                    table: Optional[List[Tuple[int, float]]] = None
                    ) -> Optional[InPlaySignal]:
    """
    Turn one live observation into a signal, or None.

    `trigger` picks which estimate is allowed to fire: "rule" tests the
    bettor's heuristic as stated, "model" tests the Brownian estimate, "both"
    requires each to clear the bar independently.
    """
    if not (0.0 < obs.best_bid <= obs.best_ask < 1.0):
        return None
    if (obs.best_ask - obs.best_bid) > max_spread:
        return None

    rule_yes = obs.rule_prob(table)
    model_yes = obs.model_prob

    candidates = []
    for side, cost, rule_fair, model_fair, team in (
        ("YES", obs.yes_cost, rule_yes, model_yes, obs.yes_team),
        ("NO", obs.no_cost, 1.0 - rule_yes, 1.0 - model_yes, obs.no_team),
    ):
        r_edge = rule_fair - cost - fee_buffer
        m_edge = model_fair - cost - fee_buffer
        if trigger == "rule":
            fires = r_edge >= min_edge
        elif trigger == "model":
            fires = m_edge >= min_edge
        else:
            fires = r_edge >= min_edge and m_edge >= min_edge
        if not fires:
            continue
        sig = InPlaySignal(obs=obs, side=side, team=team, cost=cost,
                           rule_fair=rule_fair, model_fair=model_fair,
                           source=trigger)
        if require_agreement and not sig.agree:
            continue
        candidates.append(sig)

    if not candidates:
        return None
    key = (lambda s: s.rule_edge) if trigger != "model" else (lambda s: s.model_edge)
    return max(candidates, key=key)


def calibrate_sigma(observations: List[Tuple[float, float, bool]],
                    lo: float = 4.0, hi: float = 30.0,
                    steps: int = 400) -> Optional[float]:
    """
    Fit sigma to logged outcomes by minimising Brier score.

    `observations` is [(lead, frac_remaining, yes_won)]. This is how the model
    stops being an assumption: feed it real settled games and let the data pick
    the volatility. Needs a few hundred observations to mean much.
    """
    if len(observations) < 20:
        return None
    best, best_score = None, float("inf")
    for i in range(steps + 1):
        s = lo + (hi - lo) * i / steps
        if s <= 0:
            continue
        brier = sum(
            (brownian_win_prob(lead, t, 0.0, s) - (1.0 if won else 0.0)) ** 2
            for lead, t, won in observations
        ) / len(observations)
        if brier < best_score:
            best, best_score = s, brier
    return best
