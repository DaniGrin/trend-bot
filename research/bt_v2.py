"""Backtest the two fixes the swing post-mortem pointed at, before building anything.

Fix 1 (the exit): the live step-trail does nothing below +2R and then gives back 2R, so a
trade must reach +3R to bank +1R. Replace it with a trail that arms at +1R and gives back
0.5R.

Fix 2 (the entry): 47% of live trades never moved 0.5R - the breakout fired into chop.
Replace the absolute breakout with a cross-sectional momentum rank, gated by the Kaufman
Efficiency Ratio so an asset is only tradable while it is actually trending.

Everything is measured on cross-sectionally demeaned returns where a signal test is involved,
and every variant is run on the same bars, same costs, same sizing.
"""
import json, time, datetime, urllib.request, os
import numpy as np, pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pxcache")
os.makedirs(CACHE, exist_ok=True)
COST = 0.0005

UNIVERSE = ["SPY", "QQQ", "DIA", "IWM", "SMH", "XLE", "XLF", "XLV", "XLU", "XLP",
            "GLD", "SLV", "USO", "TLT", "IEF", "EEM", "EFA", "FXI", "UUP", "VNQ"]


def bars(tkr, rng="10y"):
    f = os.path.join(CACHE, "%s_%s.csv" % (tkr, rng))
    if os.path.exists(f):
        return pd.read_csv(f, index_col=0, parse_dates=True)
    u = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (tkr, rng)
    j = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=60).read())
    r = j["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    idx, rows = [], []
    adj = r["indicators"].get("adjclose", [{}])[0].get("adjclose")
    for i, ts in enumerate(r["timestamp"]):
        if q["high"][i] is None or q["close"][i] is None:
            continue
        c = q["close"][i]
        k = (adj[i] / c) if (adj and adj[i]) else 1.0        # back-adjust for dividends
        idx.append(pd.Timestamp(datetime.date.fromtimestamp(ts)))
        rows.append((q["open"][i] * k, q["high"][i] * k, q["low"][i] * k, c * k))
    d = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])
    d.to_csv(f)
    time.sleep(0.25)
    return d


def atr(d, n=14):
    pc = d.close.shift(1)
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def efficiency_ratio(c, n=30):
    """Kaufman ER: net move / total path. 1.0 = a straight line, 0 = pure chop."""
    net = (c - c.shift(n)).abs()
    path = c.diff().abs().rolling(n).sum()
    return net / path.replace(0, np.nan)


def load():
    H, L, C, A, ER = {}, {}, {}, {}, {}
    for t in UNIVERSE:
        try:
            d = bars(t)
            if len(d) < 600:
                print("  thin, skipped:", t, len(d)); continue
            H[t], L[t], C[t] = d.high, d.low, d.close
            A[t] = atr(d)
            ER[t] = efficiency_ratio(d.close)
        except Exception as e:
            print("  failed:", t, str(e)[:60])
    f = lambda x: pd.DataFrame(x).sort_index()
    return f(H), f(L), f(C), f(A), f(ER)


def run(C, H, L, A, ER, *, k=3, risk=0.0075, atr_mult=3.0, rebal=10,
        arm=1.0, give=0.5, gate=None, label=""):
    """Cross-sectional long/short with a chandelier trail. gate = ER percentile floor, or None."""
    eq, pos, trades = 10000.0, {}, []
    mom = np.log(C).diff(60)
    erq = ER.rolling(504, min_periods=120).rank(pct=True)     # each asset's ER vs its own 2y history
    idx = C.index
    equity_curve = []
    for i in range(520, len(idx)):
        day = idx[i]
        # --- manage open positions on today's bar ---
        for t in list(pos):
            p = pos[t]
            hi, lo = H[t].iloc[i], L[t].iloc[i]
            if np.isnan(hi):
                continue
            if p["side"] == 1:
                if lo <= p["stop"]:
                    r = (p["stop"] - p["entry"]) / p["R"]
                    eq += r * p["R"] * p["qty"] - 2 * COST * p["entry"] * p["qty"]
                    trades.append(r); del pos[t]; continue
                p["peak"] = max(p["peak"], (hi - p["entry"]) / p["R"])
            else:
                if hi >= p["stop"]:
                    r = (p["entry"] - p["stop"]) / p["R"]
                    eq += r * p["R"] * p["qty"] - 2 * COST * p["entry"] * p["qty"]
                    trades.append(r); del pos[t]; continue
                p["peak"] = max(p["peak"], (p["entry"] - lo) / p["R"])
            if p["peak"] >= arm:                              # chandelier: continuous, not stepped
                p["stop"] = p["entry"] + p["side"] * (p["peak"] - give) * p["R"]
        # --- rebalance ---
        if (i - 520) % rebal == 0:
            s = mom.iloc[i - 1].copy()
            if gate is not None:
                ok = erq.iloc[i - 1] >= gate
                s = s[ok.reindex(s.index).fillna(False)]
            s = s.dropna()
            s = s[[t for t in s.index if not np.isnan(A[t].iloc[i]) and A[t].iloc[i] > 0]]
            if len(s) >= 2 * k:
                s = s - s.mean()                               # cross-sectional demean
                order = s.sort_values(ascending=False).index
                longs, shorts = set(order[:k]), set(order[-k:])
            else:
                longs, shorts = set(), set()
            for t in list(pos):
                side = pos[t]["side"]
                if (side == 1 and t not in longs) or (side == -1 and t not in shorts):
                    p = pos[t]
                    px = C[t].iloc[i]
                    r = p["side"] * (px - p["entry"]) / p["R"]
                    eq += r * p["R"] * p["qty"] - 2 * COST * p["entry"] * p["qty"]
                    trades.append(r); del pos[t]
            for t in longs | shorts:
                if t in pos:
                    continue
                R = atr_mult * A[t].iloc[i]
                px = C[t].iloc[i]
                if R <= 0 or np.isnan(px):
                    continue
                side = 1 if t in longs else -1
                pos[t] = {"side": side, "entry": px, "R": R, "peak": 0.0,
                          "qty": (eq * risk) / R, "stop": px - side * R}
        equity_curve.append(eq)
    tr = np.array(trades)
    ec = np.array(equity_curve)
    dd = (ec / np.maximum.accumulate(ec) - 1).min() * 100
    se = tr.std() / np.sqrt(len(tr)) if len(tr) else np.nan
    yrs = len(ec) / 252
    cagr = ((ec[-1] / 10000) ** (1 / yrs) - 1) * 100
    print("  %-44s  n=%4d  expR %+6.3f (t=%+5.2f)  WR %3.0f%%  final %+7.1f%%  CAGR %+5.1f%%  DD %6.1f%%"
          % (label, len(tr), tr.mean(), tr.mean() / se, 100 * (tr > 0).mean(),
             (ec[-1] / 10000 - 1) * 100, cagr, dd))
    return ec, tr


def main():
    print("loading 10y daily bars for %d assets..." % len(UNIVERSE))
    C, L_, H_, A, ER = None, None, None, None, None
    H, L, C, A, ER = load()
    C = C.dropna(how="all")
    print("  %d assets, %s .. %s\n" % (len(C.columns), C.index[0].date(), C.index[-1].date()))

    print("=" * 112)
    print("STEP 1 - THE EXIT, on identical entries (cross-sectional momentum, no regime gate)")
    print("=" * 112)
    for lbl, a, g in (("LIVE rule: arm 2R, give back 2R", 2.0, 2.0),
                      ("arm 1.5R, give back 1R", 1.5, 1.0),
                      ("arm 1R, give back 1R", 1.0, 1.0),
                      ("arm 1R, give back 0.5R  <-- post-mortem pick", 1.0, 0.5),
                      ("arm 0.5R, give back 0.5R", 0.5, 0.5)):
        run(C, H, L, A, ER, arm=a, give=g, gate=None, label=lbl)

    print("\n" + "=" * 112)
    print("STEP 2 - THE REGIME GATE, with the fixed exit (arm 1R / give 0.5R)")
    print("=" * 112)
    for g in (None, 0.4, 0.5, 0.6, 0.7):
        run(C, H, L, A, ER, arm=1.0, give=0.5, gate=g,
            label="ER percentile floor = %s" % ("none" if g is None else "%.0f%%" % (g * 100)))

    print("\n" + "=" * 112)
    print("STEP 3 - head to head: what the swing bot does vs the proposed bot")
    print("=" * 112)
    old, _ = run(C, H, L, A, ER, arm=2.0, give=2.0, gate=None, label="OLD  (live swing logic)")
    new, _ = run(C, H, L, A, ER, arm=1.0, give=0.5, gate=0.5, label="NEW  (fixed exit + regime gate)")

    print("\n" + "=" * 112)
    print("STEP 4 - year by year (the only test that matters: does it hold up out of sample?)")
    print("=" * 112)
    idx = C.index[520:520 + len(old)]
    so = pd.Series(old, index=idx); sn = pd.Series(new, index=idx)
    print("  %-6s %12s %12s" % ("year", "OLD", "NEW"))
    for y, g in so.groupby(so.index.year):
        a = (g.iloc[-1] / g.iloc[0] - 1) * 100
        h = sn.loc[g.index]
        b = (h.iloc[-1] / h.iloc[0] - 1) * 100
        print("  %-6d %11.1f%% %11.1f%%" % (y, a, b))


if __name__ == "__main__":
    main()
