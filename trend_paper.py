"""TREND BOT - cross-asset trend rotation with a regime gate. Paper money only.

This is the 5th bot, and the first one built from a post-mortem instead of from an idea.
It exists because the swing bot lost 16 of its first 17 trades, and the autopsy found two
separate diseases.

  DISEASE 1 - THE EXIT. The old step-trail did nothing at all below +2R, then gave back 2R.
  A trade had to reach +3R just to bank +1R. On the live trades, FIVE ran past +1R in our
  favour and still lost a full -1R because the trail had never armed, and all three that
  reached +2R gave every bit of it back. The old rule scored -0.765R/trade - the worst of
  ten exits tested on those same entries.
      FIX: the trail arms at +1R and gives back 0.5R, continuously, not in steps.

  DISEASE 2 - THE ENTRY. 47% of live trades never moved even 0.5R in our favour; median
  favourable excursion was 0.28R. A Donchian breakout fires on a single touch of a high,
  which in a sideways market is exactly the worst moment to buy. No exit rule can rescue
  that - the best of the ten still lost money on those entries.
      FIX: no breakouts. Rank the whole universe by 250-day momentum and hold the leaders,
      but only while an asset is genuinely trending, measured by the Kaufman Efficiency
      Ratio against that asset's own 2-year history.

WHAT THE BACKTEST SAYS (10 years, 20 ETFs, 0.05% round-trip costs, in research/):
      long-only, 250d momentum, ER gate     CAGR +9.5%   maxDD -5.6%   Sharpe 1.39
      8 of 9 years positive, worst year -1.0%, and +1.9% through the 2022 bear market
      beta to SPY +0.03 - this is NOT a repackaged stock market

AND WHAT IT DOES NOT SAY: it badly trails buy-and-hold in bull years (2021: +6.0% against
SPY's +30.5%). This is not a better way to own stocks. It is a low-drawdown, low-correlation
return stream that happens to keep working when stocks do not.

TWO ARMS, because one number proves nothing:
      main - long only, the configuration the backtest picked
      ls   - the same thing with shorts as well, so it is market-neutral by construction
If `main` beats `ls` only while stocks rise, `main` was a bull-market artifact and `ls` is
the honest number. Running both is the only way to find out.

  python trend_paper.py --once     # one daily tick (what GitHub Actions runs)
  python trend_paper.py --dry      # rank and report, trade nothing
  python trend_paper.py --reset    # wipe both accounts back to $10,000
"""
import os, sys, json, time, argparse, datetime, traceback, urllib.request
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"}
COST = 0.0005          # 0.05% round trip
ARMS = ("main", "ls")

# The 20 ETFs the backtest was run on - five asset classes, all deeply liquid.
# Nothing is added here that was not tested; an untested ticker is an untested strategy.
UNIVERSE = ["SPY", "QQQ", "DIA", "IWM", "SMH",          # US equity
            "XLE", "XLF", "XLV", "XLU", "XLP",          # sectors
            "GLD", "SLV", "USO",                        # commodities
            "TLT", "IEF",                               # bonds
            "EEM", "EFA", "FXI",                        # international
            "UUP", "VNQ"]                               # dollar, real estate

MOM_DAYS   = 250       # 12-month momentum: beat 60d and 120d across every k in the grid
ER_DAYS    = 30        # Kaufman efficiency ratio window
ER_HIST    = 504       # rank today's ER against this many days of the asset's own history
ER_FLOOR   = 0.50      # only trade an asset above the 50th percentile of its own trendiness
ARM_AT     = 1.0       # the trail arms here ...
GIVE_BACK  = 0.5       # ... and never gives back more than this from the peak
ATR_MULT   = 3.0       # R = 3 x ATR(14): wide enough that fees stay negligible
REBAL_DAYS = 10
LEGS       = 4
RISK       = 0.0075    # per leg; 4 legs long-only = 3% gross risk


# ----------------------------------------------------------------- plumbing
def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(os.path.join(HERE, "trend_bot.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sfile(arm):
    return os.path.join(HERE, "state_%s.json" % arm)


def fresh(cap):
    return {"equity": cap, "pos": {}, "last_day": "", "day_count": 0,
            "trades": 0, "wins": 0, "gross_R": 0.0}


def load_state(arm, cap):
    if os.path.exists(sfile(arm)):
        s = json.load(open(sfile(arm)))
        s.setdefault("gross_R", 0.0)
        return s
    return fresh(cap)


def save_state(arm, s):
    json.dump(s, open(sfile(arm), "w"), indent=2)


# ----------------------------------------------------------------- prices
def fetch(tkr, rng="5y", tries=3):
    """Yahoo's chart endpoint directly: no API key, no yfinance dependency, and it keeps
    working when yfinance's cookie/crumb dance breaks (which it does)."""
    url = ("https://query%d.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d")
    last = None
    for attempt in range(tries):
        host = 1 if attempt % 2 == 0 else 2
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(url % (host, tkr, rng), headers=UA), timeout=45).read()
            r = json.loads(raw)["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            adj = r["indicators"].get("adjclose", [{}])[0].get("adjclose")
            idx, rows = [], []
            for i, ts in enumerate(r["timestamp"]):
                if q["high"][i] is None or q["close"][i] is None:
                    continue
                c = q["close"][i]
                k = (adj[i] / c) if (adj and adj[i]) else 1.0     # back-adjust dividends
                idx.append(pd.Timestamp(datetime.date.fromtimestamp(ts)))
                rows.append((q["high"][i] * k, q["low"][i] * k, c * k))
            return pd.DataFrame(rows, index=idx, columns=["high", "low", "close"])
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("fetch %s failed: %s" % (tkr, last))


def atr(d, n=14):
    pc = d.close.shift(1)
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def efficiency_ratio(c, n=ER_DAYS):
    """Kaufman ER = net move / total path travelled. 1.0 is a straight line, 0 is pure chop."""
    return (c - c.shift(n)).abs() / c.diff().abs().rolling(n).sum().replace(0, np.nan)


def gather():
    out = {}
    for t in UNIVERSE:
        try:
            d = fetch(t)
            if len(d) < ER_HIST + 40:
                log("  %-5s only %d bars - skipped" % (t, len(d)))
                continue
            er = efficiency_ratio(d.close)
            out[t] = {
                "bars": d,
                "close": float(d.close.iloc[-1]),
                "atr": float(atr(d).iloc[-1]),
                "mom": float(np.log(d.close.iloc[-1] / d.close.iloc[-1 - MOM_DAYS])),
                "er_pct": float(er.rolling(ER_HIST, min_periods=120).rank(pct=True).iloc[-1]),
                "date": str(d.index[-1].date()),
            }
        except Exception as e:
            log("  %-5s FAILED: %s" % (t, str(e)[:70]))
        time.sleep(0.25)
    return out


# ----------------------------------------------------------------- trading
def close_pos(s, t, px, reason, arm):
    p = s["pos"].pop(t)
    r = p["side"] * (px - p["entry"]) / p["R"]
    pnl = r * p["R"] * p["qty"] - 2 * COST * p["entry"] * p["qty"]
    s["equity"] += pnl
    s["trades"] += 1
    s["gross_R"] += r
    if pnl > 0:
        s["wins"] += 1
    log("  [%-4s] EXIT  %-5s %-5s @ %9.3f  %-8s %+5.2fR  PnL %+8.2f  equity %9.2f  (WR %d/%d)"
        % (arm, t, "LONG" if p["side"] == 1 else "SHORT", px, reason, r, pnl,
           s["equity"], s["wins"], s["trades"]))


def open_pos(s, t, side, px, R, arm):
    qty = (s["equity"] * RISK) / R
    s["pos"][t] = {"side": side, "entry": px, "R": R, "qty": qty, "peak": 0.0,
                   "stop": px - side * R, "opened": time.strftime("%Y-%m-%d")}
    log("  [%-4s] ENTER %-5s %-5s @ %9.3f  R %7.3f  qty %9.3f  stop %9.3f"
        % (arm, t, "LONG" if side == 1 else "SHORT", px, R, qty, px - side * R))


def walk_bars(s, data, arm):
    """Replay every daily bar since the last processed day, so a missed run never loses a stop.

    The trail arms at +1R and trails 0.5R behind the peak - continuously. This is the whole
    point of the rewrite: the old rule ignored everything below +2R and then handed back 2R.
    """
    last = s["last_day"]
    for t in list(s["pos"]):
        if t not in data:
            continue
        p = s["pos"][t]
        d = data[t]["bars"]
        for ts, row in d.iterrows():
            day = str(ts.date())
            if day <= last or day <= p["opened"]:
                continue
            hi, lo = float(row.high), float(row.low)
            if p["side"] == 1:
                if lo <= p["stop"]:
                    close_pos(s, t, p["stop"], "stop", arm)
                    break
                p["peak"] = max(p["peak"], (hi - p["entry"]) / p["R"])
            else:
                if hi >= p["stop"]:
                    close_pos(s, t, p["stop"], "stop", arm)
                    break
                p["peak"] = max(p["peak"], (p["entry"] - lo) / p["R"])
            if p["peak"] >= ARM_AT:
                p["stop"] = p["entry"] + p["side"] * (p["peak"] - GIVE_BACK) * p["R"]


def rank(data, shorts):
    """Cross-sectionally demeaned 250d momentum, over the assets the regime gate lets through."""
    ok = {t: q for t, q in data.items()
          if not np.isnan(q["er_pct"]) and q["er_pct"] >= ER_FLOOR
          and q["atr"] > 0 and not np.isnan(q["mom"])}
    gated_out = sorted(set(data) - set(ok))
    if len(ok) < (2 * LEGS if shorts else LEGS):
        return set(), set(), gated_out, {}
    mu = np.mean([q["mom"] for q in ok.values()])
    sc = {t: q["mom"] - mu for t, q in ok.items()}
    order = sorted(sc, key=lambda t: sc[t], reverse=True)
    longs = set(order[:LEGS])
    short = set(order[-LEGS:]) if shorts else set()
    return longs, short, gated_out, sc


def rebalance(s, data, arm, shorts):
    longs, short, gated_out, sc = rank(data, shorts)
    if not longs:
        log("  [%-4s] only %d assets pass the regime gate - staying flat"
            % (arm, len(data) - len(gated_out)))
        return
    log("  [%-4s] gate blocked %d/%d as too choppy: %s"
        % (arm, len(gated_out), len(data), ", ".join(gated_out) if gated_out else "-"))
    log("  [%-4s] LONG  %s" % (arm, ", ".join("%s(%+.2f)" % (t, sc[t]) for t in
                                              sorted(longs, key=lambda t: -sc[t]))))
    if shorts:
        log("  [%-4s] SHORT %s" % (arm, ", ".join("%s(%+.2f)" % (t, sc[t]) for t in
                                                  sorted(short, key=lambda t: sc[t]))))
    for t in list(s["pos"]):
        side = s["pos"][t]["side"]
        if (side == 1 and t not in longs) or (side == -1 and t not in short):
            if t in data:
                close_pos(s, t, data[t]["close"], "rotated", arm)
    for t in longs | short:
        if t in s["pos"]:
            continue
        R = ATR_MULT * data[t]["atr"]
        if R <= 0:
            continue
        open_pos(s, t, 1 if t in longs else -1, data[t]["close"], R, arm)


def mark(s, data):
    u = sum(p["side"] * (data[t]["close"] - p["entry"]) * p["qty"]
            for t, p in s["pos"].items() if t in data)
    return s["equity"] + u


def append_history(day, data, s_main):
    f = os.path.join(HERE, "trend_history.csv")
    new = not os.path.exists(f)
    with open(f, "a", encoding="utf-8") as fh:
        if new:
            fh.write("date,ticker,close,atr14,mom250,er_pct,gate_pass,held_main\n")
        for t in sorted(data):
            q = data[t]
            fh.write("%s,%s,%.6f,%.6f,%.5f,%.4f,%d,%d\n"
                     % (day, t, q["close"], q["atr"], q["mom"], q["er_pct"],
                        1 if q["er_pct"] >= ER_FLOOR else 0,
                        s_main["pos"].get(t, {}).get("side", 0)))


# ----------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--capital", type=float, default=10000.0)
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--reset", action="store_true")
    a = p.parse_args()

    if a.reset:
        for arm in ARMS:
            save_state(arm, fresh(a.capital))
        log("=== both accounts reset to $%.0f ===" % a.capital)
        return

    log("=" * 104)
    log("=== TREND BOT | %d assets | mom %dd | ER gate %.0f%% | trail arms %.1fR gives %.1fR | "
        "%d legs | rebal %dd ===" % (len(UNIVERSE), MOM_DAYS, ER_FLOOR * 100, ARM_AT,
                                     GIVE_BACK, LEGS, REBAL_DAYS))

    data = gather()
    if len(data) < 2 * LEGS:
        log("only %d assets priced - aborting tick, nothing changed" % len(data))
        return
    day = max(q["date"] for q in data.values())
    passing = sum(1 for q in data.values() if q["er_pct"] >= ER_FLOOR)
    log("  bar date %s | %d assets priced | %d trending enough to trade" % (day, len(data), passing))

    if a.dry:
        for arm in ARMS:
            longs, short, out, sc = rank(data, shorts=(arm == "ls"))
            log("  [%-4s] LONG %s%s" % (arm, ", ".join(sorted(longs, key=lambda t: -sc[t])),
                                        ("   SHORT " + ", ".join(sorted(short, key=lambda t: sc[t])))
                                        if short else ""))
        log("--- dry run, nothing traded ---")
        return

    for arm in ARMS:
        s = load_state(arm, a.capital)
        if s["last_day"] == day:
            log("  [%-4s] %s already processed - skipping" % (arm, day))
            continue
        walk_bars(s, data, arm)
        s["day_count"] += 1
        if (not s["pos"]) or (s["day_count"] % REBAL_DAYS == 1):
            rebalance(s, data, arm, shorts=(arm == "ls"))
        else:
            log("  [%-4s] holding %d, next rebalance in %d days"
                % (arm, len(s["pos"]), REBAL_DAYS - (s["day_count"] % REBAL_DAYS) + 1))
        s["last_day"] = day
        save_state(arm, s)
        m = mark(s, data)
        log("  [%-4s] realized $%.2f (%+.2f%%) | mark-to-market $%.2f (%+.2f%%) | %d open | "
            "expR %+.3f over %d"
            % (arm, s["equity"], (s["equity"] / a.capital - 1) * 100, m,
               (m / a.capital - 1) * 100, len(s["pos"]),
               s["gross_R"] / max(s["trades"], 1), s["trades"]))

    append_history(day, data, load_state("main", a.capital))
    log("=== tick done ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(1)
