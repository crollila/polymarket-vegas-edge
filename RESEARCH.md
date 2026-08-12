# Research note: what 232 real prediction-market positions actually say

**Dataset.** A personal Polymarket US trading history: 516 ledger rows, 232 distinct
positions, 185 resolved, $12,615 deployed across NFL, NBA, college basketball and NHL
over roughly seven months. All figures below are reproducible with:

```bash
python analyze_history.py <export>.csv --balance <current_balance>
```

**Summary.** The account made money (+$232 realized, +167% on deposited capital), but
the record does not establish an edge: overall ROI is +6.9% with a 95% interval of
[−3.5%, +17.8%]. The prior belief going in — that college basketball was the most
profitable segment — is contradicted by the data. One hypothesis did survive testing
in a weakened form: stake size, an ex-ante variable, appears to carry information.

---

## 1. Two accounting facts that invert the answer

Neither is documented; both were established by reconciling the ledger against itself.

**A `Won` row's amount is a share count, not dollar profit.** Winning shares settle at
$1, so the payout *is* the share count — which is why every such amount is a whole
number, and why `cost / payout` recovers the average entry price. This is the only
route to entry prices in the export.

**A `Lost` row is a cost-basis write-off, not a cash movement.** The cash left the
account on the `Bought` row. Summing every `cash_flow` therefore double-counts every
loss and reports this account as **−$1,331** instead of **+$643**. Verified on 42 of 75
losing positions where `|Lost| == Bought − Sold` to the cent; the exceptions are
partial-exit positions and rows whose buys predate the export.

**Method note.** 33 positions were still open at export and are *excluded* from ROI
rather than assumed lost. Marking them to zero would swing resolved P&L from +$866 to
−$717, so the assumption is not cosmetic.

## 2. Results by sport

| Sport | Positions | Win% | Deployed | P&L | ROI | 95% CI |
|---|---|---|---|---|---|---|
| NFL | 11 | 91% | $694 | +$697 | +100.4% | [+24.9%, +162.0%] |
| NBA | 47 | 62% | $6,140 | +$300 | +4.9% | [−7.4%, +13.3%] |
| CBB | 126 | 59% | $4,772 | −$124 | −2.6% | [−19.5%, +13.4%] |
| **All** | **185** | **62%** | **$12,615** | **+$866** | **+6.9%** | **[−3.5%, +17.8%]** |

**College basketball, the segment believed to be strongest, was the weakest.** It is
also the largest sample by position count — 126 of 185 — which makes it the least
likely of these numbers to be noise.

**NFL's +100% is not a finding.** Eleven positions, with 57% of the profit in a single
Seattle Seahawks bet. The interval is wide because the sample is tiny.

Classification required exact full team names. Matching on mascot alone files "Eastern
Washington Eagles" under Philadelphia and "Eastern Illinois Panthers" under Carolina,
which contaminated the 11-position NFL sample with college results and moved its ROI by
more than 50 points before the bug was caught.

## 3. Concentration

Two Seattle Seahawks positions produced **$599 of $866** in resolved profit — 69% of
everything, from one team. A single position accounts for 46%.

Remove those two and the entire record returns **+2.4%** instead of +6.9%.

This is the dominant risk fact about the account. Whatever else is true, the headline
return is one outcome away from being flat.

## 4. The conviction hypothesis

Stake size is chosen **before** the outcome is known, which makes it a legitimate
ex-ante variable rather than a hindsight artifact. Sorting resolved positions into
stake quartiles:

| Quartile | n | Avg bet | Win% | ROI | Implied entry price | Edge vs price |
|---|---|---|---|---|---|---|
| smallest | 46 | $4.77 | 52% | −18.9% | 0.643 | **−12.2 pts** |
| 2nd | 46 | $10.88 | 57% | −9.1% | 0.622 | −5.6 pts |
| 3rd | 46 | $31.79 | 54% | −19.8% | 0.678 | −13.4 pts |
| **largest** | **47** | **$221.98** | **83%** | **+11.9%** | **0.741** | **+8.8 pts** |

Implied entry price solves `ROI = winrate / price − 1`, which separates "won often" from
"beat the price" — a high win rate on heavy favorites is expected and carries no edge.
The largest quartile beat its own entry price by ~9 points; every other quartile lost
to it.

**Significance, honestly.** The largest-quartile-minus-rest gap is +29.2 ROI points,
95% CI [+7.2, +50.5], excluding zero. But removing every Seahawks position drops the
gap to +19.2 with CI **[−0.9, +39.2] — spanning zero.** The direction survives; the
significance does not.

Add that this is one split among several examined (sport, hold-vs-sell, stake size),
and a single robustness check that flips the conclusion is not an established effect.

**Verdict: a hypothesis worth forward-testing, not a result.** It is stated here with
its failure mode attached rather than reported as the headline it superficially
resembles.

## 5. What is not testable here

- **The halftime rule** — "a team up 10+ at halftime wins ~80%, so buy below 0.80" —
  cannot be evaluated. The export has no timestamps finer than "6mo ago", no scores,
  and no in-play markers. Nothing identifies which positions were live.
  The premise is sound (a 10-point CBB halftime lead does win roughly 80%), but the
  market prices that too; the edge exists only if the market updates slower than the
  rule, and the negative CBB result is what the absence of that lag looks like.
- **Calibration.** Entry price is recoverable only on winners, because losers pay zero
  and reveal no share count. The 0.747 median entry is therefore a biased sample and no
  reliability curve can be built from this export.
- **Closing line value**, the fastest-converging edge measure, requires prices captured
  before each event closed. That data was never recorded. This is the single largest
  gap in the dataset and the reason the forward-tracking harness exists.

## 6. Conclusions

1. The account was profitable in cash terms (+$232, +167% of deposits) but the
   per-dollar edge is statistically indistinguishable from zero.
2. The strongest prior belief — college basketball — is contradicted by the largest
   sample in the dataset.
3. Returns are dangerously concentrated: 69% of profit from two positions.
4. Small stakes were systematically unprofitable (−12 points versus price in the bottom
   quartile). Whatever those bets were, they were negative-expectancy and there were
   many of them.
5. Conviction sizing is the one live hypothesis, and it is not yet significant.

**Actionable implication.** The consistent loser is the small-stake tail, and that is a
behavioral finding with a direct control: stop taking marginal positions. The live
scanner implements this as a raised edge threshold and a concentrated position cap
(`--preset conviction`), which is a hypothesis registered in advance rather than a
parameter fitted to this sample.

Forward validation, not further slicing of these 185 positions, is the way to settle
any of it.
