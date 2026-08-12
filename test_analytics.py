"""
Offline tests for the track-record math.

    python test_analytics.py

The important cases are the two simulations at the bottom: a bettor with a
known real edge, and a bettor with none who happened to win. The analysis has
to tell them apart, because that is the entire point of the module.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile

from vegasanchor.analytics import (Bet, bootstrap_roi_ci, brier_score, brier_skill_score,
                                   calibration_table, clv_summary, hit_rate, log_loss,
                                   per_bet_roi_stdev, roi, summarize)
from vegasanchor.devig import american_to_prob
from vegasanchor.tracking import PredictionStore, settle_from_scores

PASS, FAIL = 0, 0


def check(name, got, want, tol=1e-6):
    global PASS, FAIL
    if isinstance(want, float) and isinstance(got, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def bet(cost, fair, won=None, shares=100.0, closing=None):
    return Bet(label="t", cost=cost, fair=fair, shares=shares,
               stake=shares * cost, won=won, closing_fair=closing)


print("\n== profit accounting ==")
w = bet(0.40, 0.50, won=True, shares=250)     # $100 risked at +150
l = bet(0.40, 0.50, won=False, shares=250)
check("winner profit = shares*(1-cost)", w.profit, 150.0, 1e-9)
check("loser profit = -stake", l.profit, -100.0, 1e-9)
check("unsettled has no profit", bet(0.5, 0.6).profit, 0.0, 1e-9)
# An American-odds bet maps onto a contract exactly; verify against the payout.
check("+150 implies cost 0.40", american_to_prob(150), 0.40, 1e-9)
check("-150 implies cost 0.60", american_to_prob(-150), 0.60, 1e-9)

print("\n== roi / hit rate ==")
check("break-even book", roi([w, l]), 50.0 / 200.0, 1e-9)
check("hit rate", hit_rate([w, l]), 0.5, 1e-9)
check("all losses -> -100%", roi([l, bet(0.40, 0.5, won=False, shares=250)]), -1.0, 1e-9)
check("no settled bets -> None", roi([bet(0.5, 0.6)]), None)

print("\n== brier / log loss ==")
check("perfect forecast", brier_score([bet(0.5, 1.0, won=True)]), 0.0, 1e-9)
check("maximally wrong", brier_score([bet(0.5, 0.0, won=True)]), 1.0, 1e-9)
check("coin flip", brier_score([bet(0.5, 0.5, won=True)]), 0.25, 1e-9)
check("brier averages", brier_score([bet(0.5, 1.0, won=True), bet(0.5, 0.0, won=True)]), 0.5, 1e-9)
check("log loss of certainty", log_loss([bet(0.5, 1.0, won=True)]), 0.0, 1e-6)
check("no settled -> None", brier_score([bet(0.5, 0.6)]), None)

print("\n== brier skill ==")
# Forecaster that nails every outcome must beat the base rate.
perfect = [bet(0.5, 1.0, won=True), bet(0.5, 0.0, won=False)]
check("perfect forecaster has positive skill", brier_skill_score(perfect) > 0, True)
# Forecaster that is exactly the base rate has zero skill.
flat = [bet(0.5, 0.5, won=True), bet(0.5, 0.5, won=False)]
check("base-rate forecaster has ~zero skill", abs(brier_skill_score(flat)) < 1e-9, True)

print("\n== calibration ==")
# 10 bets predicted at 0.7; exactly 7 win -> perfectly calibrated bucket.
cal_bets = [bet(0.7, 0.7, won=(i < 7)) for i in range(10)]
rows = calibration_table(cal_bets, buckets=5)
check("one populated bucket", len(rows), 1)
check("predicted 0.70", rows[0]["predicted"], 0.70, 1e-9)
check("actual 0.70", rows[0]["actual"], 0.70, 1e-9)
check("counts all ten", rows[0]["n"], 10)
check("p=1.0 lands in the last bucket",
      len(calibration_table([bet(0.5, 1.0, won=True)], buckets=5)), 1)

print("\n== clv ==")
c = clv_summary([bet(0.60, 0.65, closing=0.65), bet(0.50, 0.55, closing=0.54)])
check("mean clv", c["mean"], ((0.65 - 0.60) + (0.54 - 0.50)) / 2, 1e-9)
check("beat the close every time", c["beat_rate"], 1.0, 1e-9)
check("negative clv detected", clv_summary([bet(0.60, 0.5, closing=0.55)])["mean"], -0.05, 1e-9)
check("no closing lines -> None", clv_summary([bet(0.5, 0.6)]), None)

print("\n== bootstrap ci ==")
random.seed(0)
even = [bet(0.5, 0.5, won=(i % 2 == 0)) for i in range(200)]
lo, hi = bootstrap_roi_ci(even)
check("break-even CI contains zero", lo <= 0 <= hi, True)
allwin = [bet(0.5, 0.5, won=True) for _ in range(50)]
lo2, hi2 = bootstrap_roi_ci(allwin)
check("all-winners CI excludes zero", lo2 > 0, True)
check("too few bets -> None", bootstrap_roi_ci([w]), None)

print("\n== store round-trip ==")
tmp = os.path.join(tempfile.mkdtemp(), "p.jsonl")
store = PredictionStore(tmp)
rid = store.log_prediction(market_slug="m1", game="DET vs CIN", side="NO",
                           bet_on="Cincinnati Bengals", cost=0.73, fair=0.83,
                           edge=0.10, shares=34, stake=24.82,
                           kickoff="2026-09-14T17:00:00Z")
check("logged one", len(store.load()), 1)
check("dedupe detects the pair", store.already_logged("m1", "NO"), True)
check("different side is not a dupe", store.already_logged("m1", "YES"), False)
store.record_close(rid, 0.80, -400)
store.record_settlement(rid, True, "detail")
merged = store.load()
check("append-only merges to one record", len(merged), 1)
check("closing survived the merge", merged[0]["closing_fair"], 0.80, 1e-9)
check("settlement survived the merge", merged[0]["won"], True)
check("original cost preserved", merged[0]["cost"], 0.73, 1e-9)
b = store.to_bets()[0]
check("converts to a Bet", b.won, True)
check("clv computed from the merge", b.clv, 0.80 - 0.73, 1e-9)

print("\n== fixture disambiguation (a team plays many games a season) ==")
# Tennessee appears in a preseason game AND a regular-season game. Matching on
# the backed team alone graded the preseason bet against the week-1 line.
cands = {
    frozenset(("Tennessee Titans", "San Francisco 49ers")): "preseason",
    frozenset(("Tennessee Titans", "Denver Broncos")): "week1",
}
from vegasanchor.tracking import match_by_pair, record_team_pair
rec_pre = {"bet_on": "Tennessee Titans",
           "teams": ["Tennessee Titans", "San Francisco 49ers"]}
check("pair picks the right fixture", match_by_pair(rec_pre, cands, "Tennessee Titans"), "preseason")
rec_wk1 = {"bet_on": "Tennessee Titans", "teams": ["Tennessee Titans", "Denver Broncos"]}
check("pair picks the other fixture", match_by_pair(rec_wk1, cands, "Tennessee Titans"), "week1")
rec_bare = {"bet_on": "Tennessee Titans", "teams": []}
check("ambiguous single-team match refuses",
      match_by_pair(rec_bare, cands, "Tennessee Titans"), None)
solo = {frozenset(("Tennessee Titans", "San Francisco 49ers")): "only"}
check("unambiguous single-team match still works",
      match_by_pair(rec_bare, solo, "Tennessee Titans"), "only")
check("pair extraction needs exactly two", record_team_pair({"teams": ["Detroit Lions"]}), None)
check("unknown team names do not form a pair",
      record_team_pair({"teams": ["Detroit Lions", "Not A Team"]}), None)

print("\n== settlement from scores ==")
tmp2 = os.path.join(tempfile.mkdtemp(), "p2.jsonl")
s2 = PredictionStore(tmp2)
s2.log_prediction(market_slug="m2", game="DET vs CIN", side="NO",
                  bet_on="Cincinnati Bengals", cost=0.73, fair=0.83, edge=0.1,
                  shares=10, stake=7.3, kickoff="2020-01-01T00:00:00Z")
scores = [{"completed": True, "home_team": "Cincinnati Bengals",
           "away_team": "Detroit Lions",
           "scores": [{"name": "Cincinnati Bengals", "score": "24"},
                      {"name": "Detroit Lions", "score": "17"}]}]
check("settles a finished game", settle_from_scores(s2, scores), 1)
check("backed team won", s2.load()[0]["won"], True)

s3 = PredictionStore(os.path.join(tempfile.mkdtemp(), "p3.jsonl"))
s3.log_prediction(market_slug="m3", game="g", side="YES", bet_on="Detroit Lions",
                  cost=0.5, fair=0.5, edge=0, shares=1, stake=0.5,
                  kickoff="2020-01-01T00:00:00Z")
check("loser marked correctly", (settle_from_scores(s3, scores), s3.load()[0]["won"]), (1, False))
check("unfinished game is left open",
      settle_from_scores(s3, [{**scores[0], "completed": False}]), 0)
tie = [{"completed": True, "home_team": "Cincinnati Bengals", "away_team": "Detroit Lions",
        "scores": [{"name": "Cincinnati Bengals", "score": "20"},
                   {"name": "Detroit Lions", "score": "20"}]}]
s4 = PredictionStore(os.path.join(tempfile.mkdtemp(), "p4.jsonl"))
s4.log_prediction(market_slug="m4", game="g", side="YES", bet_on="Detroit Lions",
                  cost=0.5, fair=0.5, edge=0, shares=1, stake=0.5,
                  kickoff="2020-01-01T00:00:00Z")
check("a tie is not guessed at", settle_from_scores(s4, tie), 0)

# --------------------------------------------------------------------------
# The cases that matter: can this tell edge from luck?
# --------------------------------------------------------------------------
print("\n== simulation: a bettor with a REAL 4-point edge ==")
rng = random.Random(42)
skilled = []
for _ in range(400):
    close = rng.uniform(0.35, 0.75)      # true probability
    # Bought ~4 points cheap on average, with realistic execution noise.
    cost = min(max(close - rng.gauss(0.04, 0.02), 0.02), 0.97)
    won = rng.random() < close
    skilled.append(Bet(label="s", cost=cost, fair=close, shares=100,
                       stake=100 * cost, won=won, closing_fair=close))
ss = summarize(skilled)
check("positive ROI", ss["roi"] > 0, True)
check("CLV mean ~ +4 pts", abs(ss["clv"]["mean"] - 0.04) < 0.01, True)
check("CLV t-stat clears 2 easily", ss["clv"]["t_stat"] > 5, True)

# The headline comparison. Whether ROI happens to reach significance at n=400
# swings on the luck of the draw, but the ORDERING never does: CLV extracts
# the same edge with a far sharper signal, because it is not diluted by the
# coin-flip variance of who actually won. That ratio is the whole argument for
# tracking closing lines instead of waiting on P&L.
import math as _m
_roi_t = ss["roi"] / (ss["roi_stdev"] / _m.sqrt(ss["settled"]))
check("CLV signal is far sharper than ROI signal", ss["clv"]["t_stat"] > 3 * _roi_t, True)
print(f"       ROI {ss['roi']*100:+.1f}%  (t={_roi_t:.1f})   "
      f"CLV {ss['clv']['mean']*100:+.2f}pts  (t={ss['clv']['t_stat']:.1f})")
print(f"       ^ same 400 bets, same real edge. CLV's t-stat is "
      f"{ss['clv']['t_stat']/_roi_t:.0f}x the ROI t-stat.")
print(f"         bets needed to prove it on ROI alone: ~{ss['n_for_significance']:,}")

print("\n== simulation: NO edge, but got lucky ==")
# Fair prices, no edge. Keep drawing until we find a run that shows a profit --
# exactly the record someone remembers as "I made a lot of money".
lucky = None
for seed in range(200):
    r2 = random.Random(seed)
    trial = []
    for _ in range(60):
        close = r2.uniform(0.35, 0.75)
        won = r2.random() < close
        trial.append(Bet(label="l", cost=close, fair=close, shares=100,
                         stake=100 * close, won=won, closing_fair=close))
    if roi(trial) > 0.06:
        lucky = trial
        break
ls = summarize(lucky)
check("found a profitable no-edge run", ls["roi"] > 0.06, True)
check("but CLV is ~zero", abs(ls["clv"]["mean"]) < 0.005, True)
check("and CLV t-stat is small", abs(ls["clv"]["t_stat"]) < 2, True)
check("and the ROI CI spans zero", ls["roi_ci"][0] <= 0 <= ls["roi_ci"][1], True)
print(f"       ROI {ls['roi']*100:+.1f}%  CLV {ls['clv']['mean']*100:+.2f}pts  "
      f"t={ls['clv']['t_stat']:.1f}  CI[{ls['roi_ci'][0]*100:+.1f}%,{ls['roi_ci'][1]*100:+.1f}%]")
print("       ^ profitable on paper, no evidence of edge. This is the case the")
print("         whole module exists to catch.")

print(f"\n{'='*44}\n{PASS} passed, {FAIL} failed\n{'='*44}")
sys.exit(1 if FAIL else 0)
