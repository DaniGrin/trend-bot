# trend-bot — cross-asset trend rotation with a regime gate

Paper money only. No API keys, no exchange account, no credit card. Runs free on GitHub
Actions, once a day after the US close (22:15 UTC, Mon–Fri).

This is the fifth bot, and the first one built from a **post-mortem** rather than from an idea.

## Why it exists

The swing bot lost **16 of its first 17 trades**. The autopsy (`research/diagnose_swing.py`,
run against the real live logs) found two separate diseases.

### Disease 1 — the exit

The old step-trail did nothing at all below **+2R**, then gave back 2R. A trade had to reach
**+3R just to bank +1R**. On the real trades:

- **five** ran past +1R in our favour and still lost a full −1R, because the trail had never armed
- **all three** that reached +2R gave back every bit of it (2.1R, 2.1R, 2.8R)
- not one trade in 17 exited above +1R

Replayed against ten different exit rules on those same entries, the live rule came **last**:

| exit rule | expR/trade |
|---|---|
| **live: arms at 2R, gives back 2R** | **−0.765** |
| fixed 2R target | −0.471 |
| arms at 1R, gives back 1R | −0.412 |
| fixed 1R target | −0.294 |
| **arms at 1R, gives back 0.5R** | **−0.292** |

**Fix:** the trail arms at **+1R** and trails **0.5R** behind the peak, continuously rather
than in steps.

### Disease 2 — the entry

**47%** of live trades never moved even 0.5R in our favour. Median favourable excursion:
**0.28R**. A Donchian breakout fires on a single touch of a high, which in a sideways market
is precisely the worst moment to buy. Note that even the *best* of the ten exits above is
still negative — **no exit rule rescues these entries.**

**Fix:** no breakouts. Rank the universe by **250-day momentum** and hold the leaders, but
only while an asset is genuinely trending — measured by the **Kaufman Efficiency Ratio**
against that asset's own 2-year history.

## What the backtest says

10 years, 20 ETFs, 0.05% round-trip costs (`research/bt_v2.py`):

| | CAGR | max DD | Sharpe | beta to SPY |
|---|---|---|---|---|
| **long-only, 250d, ER-gated** | **+9.5%** | **−5.6%** | **1.39** | **+0.03** |
| long/short, 250d, ER-gated | +4.2% | −10.6% | 0.52 | +0.04 |
| buy & hold SPY | +16.0% | −33.7% | 0.86 | 1.00 |

**8 of 9 years positive, worst year −1.0%, and +1.9% through the 2022 bear market.** Beta of
+0.03 means this is not a repackaged stock market.

The regime gate beat the ungated version in **all 9 cells** of the lookback × legs grid — a
consistent pattern rather than one lucky parameter.

### And what it does not say

It **badly trails buy-and-hold in bull years** — 2021: +6.0% against SPY's +30.5%; 2023:
+3.1% against +26.7%. This is not a better way to own stocks. It is a low-drawdown,
low-correlation return stream that happens to keep working when stocks do not. The 2022
result is the point of the whole thing, not the 2021 one.

## Two arms

One number proves nothing, so the bot runs two $10,000 accounts:

| account | what |
|---|---|
| `main` | long only — the configuration the backtest picked |
| `ls` | the same thing with shorts too, so it is market-neutral by construction |

If `main` beats `ls` only while stocks rise, `main` was a bull-market artifact and `ls` is the
honest number. Running both is the only way to find out.

## Configuration

```
universe     20 ETFs: SPY QQQ DIA IWM SMH | XLE XLF XLV XLU XLP | GLD SLV USO
             | TLT IEF | EEM EFA FXI | UUP VNQ
momentum     250 days, cross-sectionally demeaned
regime gate  Kaufman ER(30), must be >= the 50th percentile of its own 2y history
entry        4 legs per side, equal risk
R            3 x ATR(14)
risk         0.75% of equity per leg
exit         stop at 1R; trail arms at +1R and trails 0.5R behind the peak
rebalance    every 10 trading days
costs        0.05% round trip
```

Nothing is in the universe that was not backtested — an untested ticker is an untested
strategy.

> **Why daily bars.** Required gross edge just to break even is `2 × cost × (entry / R)`. An
> intraday stop makes `entry/R` enormous (~270) and fees swamp any edge — that is what killed
> the ORB bot at 229 trades. A 3×ATR daily stop keeps the drag negligible.

## Data

Prices come from Yahoo's chart endpoint directly over `urllib` — no API key, and no `yfinance`
dependency, which matters because yfinance's cookie/crumb handling breaks periodically while
the raw endpoint keeps working. Only `pandas` and `numpy` are needed.

`trend_history.csv` grows one row per asset per day: close, ATR, momentum, ER percentile,
whether the gate passed, and what `main` held. That is the record needed to audit any future
claim about why a trade happened.

## Files

| file | what |
|---|---|
| `trend_paper.py` | the whole bot |
| `state_main.json`, `state_ls.json` | the two accounts |
| `trend_history.csv` | daily per-asset signal record |
| `trend_bot.log` | every gate decision, entry and exit |
| `research/diagnose_swing.py` | the post-mortem on the live swing trades |
| `research/bt_v2.py` | the 10-year backtest behind the configuration |

## Running it yourself

```bash
pip install pandas numpy
python trend_paper.py --dry      # rank and report, trade nothing
python trend_paper.py --once     # one daily tick
python trend_paper.py --reset    # wipe both accounts back to $10,000
```
