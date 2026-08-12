"""
Was it edge, or was it variance?

A betting record with a positive return proves very little on its own. These
are the measurements that separate the two explanations:

  CLV   Closing line value. Did you get a better price than the market's final
        one? This is the leading indicator -- it converges far faster than P&L,
        because it does not have to wait for coin flips to average out. A
        bettor with real edge beats the close consistently; a lucky one does not.

  Brier Calibration. When you say 70%, does it happen 70% of the time? A model
        can be profitable and badly calibrated (or the reverse), and knowing
        which tells you whether to trust its sizing.

  ROI + bootstrap CI. The honest version of "I made money": a point estimate
        with an interval around it. If the interval spans zero, the record is
        consistent with having no edge at all, no matter how green the total is.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class Bet:
    """One resolved (or pending) wager, normalized to prediction-market terms."""
    label: str
    cost: float                     # price paid per share, 0..1
    fair: float                     # your predicted probability at bet time
    shares: float = 0.0
    stake: float = 0.0
    won: Optional[bool] = None      # None while unsettled
    closing_fair: Optional[float] = None   # market's final fair prob for this side
    kickoff: str = ""
    source: str = "bot"

    @property
    def settled(self) -> bool:
        return self.won is not None

    @property
    def profit(self) -> float:
        """Payout minus cost. A winning share pays $1."""
        if self.won is None:
            return 0.0
        return self.shares * (1.0 - self.cost) if self.won else -self.shares * self.cost

    @property
    def clv(self) -> Optional[float]:
        """
        Closing line value in probability points.

        You bought at `cost`; the market closed valuing it at `closing_fair`.
        Positive means you got the better side of the final price.
        """
        if self.closing_fair is None:
            return None
        return self.closing_fair - self.cost


def brier_score(bets: Sequence[Bet]) -> Optional[float]:
    """Mean squared error of the probability forecasts. Lower is better."""
    s = [b for b in bets if b.settled]
    if not s:
        return None
    return sum((b.fair - (1.0 if b.won else 0.0)) ** 2 for b in s) / len(s)


def log_loss(bets: Sequence[Bet], eps: float = 1e-9) -> Optional[float]:
    s = [b for b in bets if b.settled]
    if not s:
        return None
    total = 0.0
    for b in s:
        p = min(max(b.fair, eps), 1 - eps)
        total += -(math.log(p) if b.won else math.log(1 - p))
    return total / len(s)


def brier_skill_score(bets: Sequence[Bet]) -> Optional[float]:
    """
    Brier relative to the naive "always predict the base rate" forecaster.

    > 0 means your probabilities carry information the base rate does not.
    """
    s = [b for b in bets if b.settled]
    if len(s) < 2:
        return None
    bs = brier_score(s)
    base = sum(1.0 for b in s if b.won) / len(s)
    ref = sum((base - (1.0 if b.won else 0.0)) ** 2 for b in s) / len(s)
    if ref == 0:
        return None
    return 1.0 - (bs / ref)


def calibration_table(bets: Sequence[Bet], buckets: int = 5) -> List[Dict[str, float]]:
    """Bucket forecasts by predicted probability and compare to what happened."""
    s = [b for b in bets if b.settled]
    rows: List[Dict[str, float]] = []
    if not s:
        return rows
    width = 1.0 / buckets
    for i in range(buckets):
        lo, hi = i * width, (i + 1) * width
        inb = [b for b in s if (lo <= b.fair < hi) or (i == buckets - 1 and b.fair == 1.0)]
        if not inb:
            continue
        rows.append({
            "lo": lo,
            "hi": hi,
            "n": len(inb),
            "predicted": sum(b.fair for b in inb) / len(inb),
            "actual": sum(1.0 for b in inb if b.won) / len(inb),
        })
    return rows


def roi(bets: Sequence[Bet]) -> Optional[float]:
    s = [b for b in bets if b.settled]
    staked = sum(b.shares * b.cost for b in s)
    if staked <= 0:
        return None
    return sum(b.profit for b in s) / staked


def hit_rate(bets: Sequence[Bet]) -> Optional[float]:
    s = [b for b in bets if b.settled]
    if not s:
        return None
    return sum(1.0 for b in s if b.won) / len(s)


def bootstrap_roi_ci(bets: Sequence[Bet], iterations: int = 10_000,
                     alpha: float = 0.05, seed: int = 1) -> Optional[Tuple[float, float]]:
    """
    Percentile bootstrap confidence interval for ROI.

    Resamples your bets with replacement. If the resulting interval includes
    zero, your record does not distinguish skill from luck at this sample size.
    """
    s = [b for b in bets if b.settled]
    if len(s) < 2:
        return None
    rng = random.Random(seed)
    n = len(s)
    out: List[float] = []
    for _ in range(iterations):
        pick = [s[rng.randrange(n)] for _ in range(n)]
        staked = sum(b.shares * b.cost for b in pick)
        if staked > 0:
            out.append(sum(b.profit for b in pick) / staked)
    if not out:
        return None
    out.sort()
    lo = out[int((alpha / 2) * len(out))]
    hi = out[min(len(out) - 1, int((1 - alpha / 2) * len(out)))]
    return lo, hi


def clv_summary(bets: Sequence[Bet]) -> Optional[Dict[str, float]]:
    """
    Aggregate closing line value, with a t-statistic against the null of zero.

    |t| > 2 with a decent sample is the strongest cheap evidence of real edge
    you can get in this domain.
    """
    vals = [b.clv for b in bets if b.clv is not None]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    beat = sum(1 for v in vals if v > 0) / n
    if n < 2:
        return {"n": n, "mean": mean, "beat_rate": beat, "stdev": 0.0, "t_stat": 0.0}
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    sd = math.sqrt(var)
    t = (mean / (sd / math.sqrt(n))) if sd > 0 else 0.0
    return {"n": n, "mean": mean, "beat_rate": beat, "stdev": sd, "t_stat": t}


def bets_needed_for_significance(observed_roi: float, roi_stdev: float,
                                 target_t: float = 2.0) -> Optional[int]:
    """
    How many bets before an ROI this size clears a t of `target_t`.

    Usually a sobering number, and the reason CLV is worth tracking instead of
    waiting on P&L.
    """
    if observed_roi <= 0 or roi_stdev <= 0:
        return None
    return int(math.ceil((target_t * roi_stdev / observed_roi) ** 2))


def per_bet_roi_stdev(bets: Sequence[Bet]) -> Optional[float]:
    """Standard deviation of per-bet return on stake."""
    s = [b for b in bets if b.settled and b.cost > 0 and b.shares > 0]
    if len(s) < 2:
        return None
    rets = [b.profit / (b.shares * b.cost) for b in s]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def summarize(bets: Sequence[Bet]) -> Dict[str, object]:
    settled = [b for b in bets if b.settled]
    staked = sum(b.shares * b.cost for b in settled)
    profit = sum(b.profit for b in settled)
    sd = per_bet_roi_stdev(settled)
    r = roi(settled)
    return {
        "total": len(bets),
        "settled": len(settled),
        "pending": len(bets) - len(settled),
        "staked": staked,
        "profit": profit,
        "roi": r,
        "hit_rate": hit_rate(settled),
        "brier": brier_score(settled),
        "brier_skill": brier_skill_score(settled),
        "log_loss": log_loss(settled),
        "roi_ci": bootstrap_roi_ci(settled),
        "clv": clv_summary(bets),
        "calibration": calibration_table(settled),
        "roi_stdev": sd,
        "n_for_significance": (bets_needed_for_significance(r, sd)
                               if (r is not None and sd is not None) else None),
    }
