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
python scan.py --json bets.json # machine-readable output
python test_strategy.py         # 66 offline checks on the math. No API key needed.
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

## Layout

```
scan.py                 read-only recommendations
trade.py                the bot loop
test_strategy.py        66 offline assertions on the decision logic
vegasanchor/
  config.py             every tunable
  devig.py              American odds -> fair probability
  teams.py              32-team registry; refuses to guess on ambiguous names
  oddsapi.py            The Odds API v4 client, median across books
  polymarket.py         gateway (public prices) + signed trading client
  edge.py               edge, Kelly sizing, order payloads
```

## Verified against the live API

Confirmed working at build time: gateway market discovery (64 NFL moneyline markets),
the BBO endpoint (which returns `marketData`, not `marketDataLite`), Ed25519 request
signing, balances, positions, and the order-preview schema — which requires the payload
wrapped as `{"request": {...}}`.
