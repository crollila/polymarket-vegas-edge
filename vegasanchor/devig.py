"""
Odds conversion and vig removal.

A sportsbook price is not a probability -- it embeds the book's margin ("vig").
Detroit -110 / Cincinnati -110 implies 52.4% + 52.4% = 104.8%. That extra 4.8%
is the house edge, and you must strip it before comparing to Polymarket, or
every single market will look like Polymarket is "cheap" on both sides.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple


def american_to_prob(american: float) -> float:
    """Convert American odds to raw (vig-inclusive) implied probability."""
    a = float(american)
    if a == 0:
        raise ValueError("American odds cannot be 0")
    if a < 0:
        return (-a) / ((-a) + 100.0)
    return 100.0 / (a + 100.0)


def prob_to_american(p: float) -> int:
    """Inverse of american_to_prob, for display."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    if p >= 0.5:
        return int(round(-100.0 * p / (1.0 - p)))
    return int(round(100.0 * (1.0 - p) / p))


def multiplicative_devig(raw: Sequence[float]) -> List[float]:
    """
    Simplest devig: scale every side down proportionally so they sum to 1.

    Fast and unbiased for near-even matchups, but it systematically overstates
    heavy favorites, because books load proportionally more vig onto longshots.
    """
    total = float(sum(raw))
    if total <= 0:
        raise ValueError("Cannot devig non-positive probabilities")
    return [p / total for p in raw]


def power_devig(raw: Sequence[float], tol: float = 1e-9, max_iter: int = 200) -> List[float]:
    """
    Power devig: find k such that sum(p_i ** k) == 1.

    This removes proportionally more vig from longshots than from favorites,
    which matches how books actually price. Preferred when you are trading
    lopsided markets (a 0.90 favorite), which is exactly this strategy's band.
    """
    probs = [max(float(p), 1e-9) for p in raw]
    total = sum(probs)
    if total <= 0:
        raise ValueError("Cannot devig non-positive probabilities")
    if abs(total - 1.0) < tol:
        return list(probs)

    # sum(p^k) is monotonically decreasing in k for p in (0,1), so bisect.
    lo, hi = 0.5, 5.0
    for _ in range(max_iter):
        if sum(p ** hi for p in probs) < 1.0:
            break
        hi *= 2.0
    else:  # pragma: no cover - defensive
        return multiplicative_devig(probs)

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        s = sum(p ** mid for p in probs)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    out = [p ** k for p in probs]
    # Guard against tiny numerical drift.
    return multiplicative_devig(out)


def devig(raw: Sequence[float], method: str = "power") -> List[float]:
    if method == "multiplicative":
        return multiplicative_devig(raw)
    if method == "power":
        return power_devig(raw)
    raise ValueError(f"Unknown devig method: {method!r}")


def fair_two_way(american_a: float, american_b: float, method: str = "power") -> Tuple[float, float]:
    """Devig a two-way moneyline into (fair_prob_a, fair_prob_b)."""
    raw = [american_to_prob(american_a), american_to_prob(american_b)]
    fair = devig(raw, method=method)
    return fair[0], fair[1]


def hold_pct(american_a: float, american_b: float) -> float:
    """The book's margin on this market, in percent. ~4-5% is normal for NFL."""
    raw = american_to_prob(american_a) + american_to_prob(american_b)
    return (raw - 1.0) * 100.0
