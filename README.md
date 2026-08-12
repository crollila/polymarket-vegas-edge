# Vegas Odds Anchor Bot 1.0

Compares **FanDuel's NFL moneyline** against **Polymarket US** prices, and tells you
what to buy — or places the order itself.

The premise: sportsbooks employ people to price NFL games and take enough action to
correct fast. Polymarket is thinner. When the two disagree by more than the cost of
trading, the book is usually the better estimate. This bot measures that gap.

```
GAME          ACTION               PM COST    BOOK   FAIR    EDGE     STAKE  SHARES      EV
DET vs CIN    BUY NO on CIN           0.73    -500   0.83   +9.5%    $24.82      34  $+3.23
```

Read that as: *buy the NO contract on the Lions market at 73¢; it pays $1 if
Cincinnati wins, and FanDuel's devigged price says that happens 83% of the time.*

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

| Key | Where to get it |
|---|---|
| `PM_API_KEY_ID`, `PM_PRIVATE_KEY_BASE64` | polymarket.us → Account → API keys |
| `ODDS_API_KEY` | [the-odds-api.com](https://the-odds-api.com) — free tier is 500 requests/month |
| `ODDS_API_BOOKMAKERS` | `fanduel`, or a comma-separated list to use their median |

## Use

```bash
python scan.py                  # what to buy right now. Read-only, places nothing.
python scan.py --all            # every matched game, including the no-bets
python scan.py --log            # same, but record the picks for CLV tracking
python scan.py --json bets.json # machine-readable output
python test_strategy.py         # 66 offline checks on the decision math
python test_analytics.py        # 57 offline checks on the track-record math
```

```bash
python trade.py                 # dry run: previews real orders, submits none
python trade.py --once          # single pass
python trade.py --live          # real money, requires typing TRADE to confirm
```

Useful flags on both: `--min-edge 0.03`, `--kelly 0.5`, `--books fanduel,pinnacle`,
`--maker` (rest orders instead of crossing the spread), `--no-live` (skip in-progress games).

## How it decides

**1. Strip the vig.** FanDuel at -110/-110 implies 52.4% + 52.4% = 104.8%. That 4.8%
is the house margin. Comparing raw book prices to Polymarket makes *every* market look
like a bargain on both sides. Default is **power devig** (solve `Σ pᵢᵏ = 1`), which
strips proportionally more from longshots — on a -1200 favorite it lands at 91.3% where
naive proportional devig says 88.7%. That 2.6-point gap is bigger than the edge
threshold, so the method genuinely matters.

**2. Price the side you'd actually pay.** A Polymarket moneyline is one YES/NO contract
with a bid/ask, not two independent prices:

| | limit price (YES basis) | you pay |
|---|---|---|
| Buy YES, taker | `best_ask` | `best_ask` |
| Buy NO, taker | `best_bid` | `1 − best_bid` |
| Buy YES, maker | `best_bid` | `best_bid` |
| Buy NO, maker | `best_ask` | `1 − best_ask` |

Buying NO is selling YES, so you hit the bid. Scoring against the midpoint instead
flatters every trade by half the spread.

**3. Require a real edge.** `edge = fair − cost − fee_buffer`, and it must clear
`min_edge` (default 4%). Markets that are too wide, too thin, or too close to
0/1 are dropped.

**4. Size with fractional Kelly.** `f* = (p − c)/(1 − c)`, scaled by `kelly_fraction`
(default 0.25) and capped at 10% of bankroll. Quarter-Kelly because the edge estimate
is itself uncertain, and full Kelly on a wrong estimate is how accounts die.

## Research

[`RESEARCH.md`](RESEARCH.md) is a post-mortem on 232 real positions from a live
Polymarket account ($12,615 deployed over seven months) — what the record supports,
what it refutes, and the one hypothesis still open. A rendered version lives in [`docs/`](docs/index.html) and is
published via GitHub Pages.

```bash
python analyze_history.py <polymarket_export>.csv --balance <current_balance>
```

Reports P&L by sport with bootstrap intervals, profit concentration, recovered entry
prices, a conviction test on stake size, and a cash reconciliation. Two undocumented
ledger conventions are handled explicitly — a `Won` amount is a share count, and a
`Lost` row is a write-off rather than a cash movement; reading it naively turns a
+$643 record into −$1,331.

## Proving the edge

A positive return proves very little. These four commands build the record that
separates edge from luck:

```bash
python track.py log       # snapshot picks at decision time
python track.py close     # capture the closing line, shortly before kickoff
python track.py settle    # mark winners from the scores feed
python track.py report    # CLV, calibration, ROI with a confidence interval
```

**Closing line value is the metric that matters.** Did you get a better price than
the market's final one? It converges far faster than P&L because it isn't diluted by
the coin-flip variance of who actually won. The test suite demonstrates this on a
simulated bettor with a *known* 4-point edge: across 400 bets, CLV lands a t-stat of
**39.9** while the ROI t-stat is **2.2** — same bets, same real edge, an 18× sharper
signal. Waiting on P&L to tell you whether a strategy works means waiting for
thousands of bets.

The complementary test is a bettor with **no** edge who got lucky: +9.3% ROI over 60
bets, CLV of exactly zero, and an ROI confidence interval of [-13.9%, +32.4%].
Profitable on paper, no evidence of skill. Catching that case is the whole point.

`report` also gives you a calibration table (when you say 70%, does it happen 70% of
the time?), Brier score and skill score, and a bootstrap CI on ROI.

### Grading bets you already placed

```bash
python track.py import my_bets.csv
python track.py report --source manual
```

See `bets_template.csv` for the format. Minimum is `bet_on`, `odds`, `stake`; add
`opponent` so a bet can be pinned to a specific week, `result` if you know it, and
`closing_odds` — that last column is what unlocks CLV, and it's the difference
between "I made money" and "I had an edge."

An American-odds wager maps exactly onto a prediction-market contract: cost per share
is the implied probability, and stake buys `stake / cost` shares. A -150 bet and a
$0.60 contract are the same position, so book bets and Polymarket bets land in one
comparable record.

### On backtesting last season

There is no backtest in this repo, and it isn't an oversight. It cannot be built from
available data:

- **Historical sportsbook odds** are paywalled on The Odds API — the free tier returns
  `HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN`.
- **Historical Polymarket US prices do not exist publicly.** No price-history, candles,
  trades, or timeseries endpoint (all 404), and querying closed or archived NFL events
  returns zero records. Last season's order books are simply gone.

Both halves of the edge calculation are unavailable, so any "2025 backtest" here would
be simulated data wearing a real result's clothing. Forward-tracked CLV is slower but
it's actually evidence.

## Safety

Live trading requires the `--live` flag *and* typing `TRADE` at a prompt. On top of
that: per-trade and position caps, one order per market per session, a preview call
against the exchange before every submit, and a `KILL_SWITCH` file — `touch KILL_SWITCH`
and the loop stops before its next order. Every decision is appended to
`trade_journal.jsonl`.

## Honest limitations

- **FanDuel is not a sharp book.** It is a retail book with retail bias. When
  Polymarket disagrees with FanDuel, that is not automatically free money — sometimes
  Polymarket is right. `--books fanduel,pinnacle,betmgm` gives a steadier anchor.
- **Stale-line risk is the main way this loses.** If FanDuel has moved and the feed
  hasn't refreshed, you are trading against your own lag, not an inefficiency. This
  is sharpest on in-play markets; `--no-live` avoids that class entirely.
- **The edges you find at 4% will mostly be in thin markets.** Open interest filters
  help, but a 20-share book at a great price is not a 200-share opportunity.
- **Devigged book probability is not truth**, it is an estimate with its own error
  bars. The EV column is what the model believes, not what you will earn.
- **Fees and slippage** are approximated by a flat 0.5% buffer, not read from the
  exchange fee schedule.
- Nothing here is financial advice, and the `min_order_usd` floor means a small
  bankroll will find edges it cannot legally act on.
- **No backtest exists** (see above) and the bot has no track record yet. Until
  `track.py report` shows a CLV t-stat above 2 on a real sample, treat every edge
  number here as a hypothesis rather than a result.

## Layout

```
scan.py                 read-only recommendations
analyze_history.py      grade a real Polymarket export
RESEARCH.md             post-mortem on 232 live positions
docs/index.html         the same note, rendered (GitHub Pages)
trade.py                the bot loop
track.py                log / close / settle / report / import
test_strategy.py        66 offline assertions on the decision logic
test_analytics.py       57 offline assertions on the track-record math
bets_template.csv       CSV format for importing bets you already placed
vegasanchor/
  config.py             every tunable
  devig.py              American odds -> fair probability
  teams.py              32-team registry; refuses to guess on ambiguous names
  oddsapi.py            The Odds API v4 client, median across books
  polymarket.py         gateway (public prices) + signed trading client
  edge.py               edge, Kelly sizing, order payloads
  analytics.py          CLV, calibration, Brier, bootstrap CI
  tracking.py           append-only prediction log and settlement
```

## Verified against the live API

Confirmed working at build time: gateway market discovery (64 NFL moneyline markets),
the BBO endpoint (which returns `marketData`, not `marketDataLite`), Ed25519 request
signing, balances, positions, and the order-preview schema — which requires the payload
wrapped as `{"request": {...}}`.
