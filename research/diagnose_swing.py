"""Why does the swing bot lose 16 of 17?

For every trade it actually took, reconstruct what the price did between entry and exit:
how far it ran in our favour (MFE) before reversing, and what a set of alternative exits
would have banked on those exact same entries. The entries are held fixed, so anything that
changes is caused by the EXIT rule alone.
"""
import re, glob, os, json, time, datetime, urllib.request
import numpy as np

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}


def bars(tkr, rng="1y"):
    u = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (tkr, rng)
    j = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=40).read())
    r = j["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(r["timestamp"]):
        if q["high"][i] is None:
            continue
        out.append((datetime.date.fromtimestamp(ts), q["open"][i], q["high"][i], q["low"][i], q["close"][i]))
    return out


def parse_trades():
    trades = []
    for f in sorted(glob.glob("swlogs/*.log")):
        t = os.path.basename(f)[:-4]
        cur = None
        for ln in open(f, encoding="utf-8", errors="ignore"):
            m = re.search(r"(\d{4}-\d\d-\d\d)\s+ENTER (LONG|SHORT) @ ([\d.]+)\s+qty ([\d.]+)\s+stop ([\d.]+)\s+\(R=([\d.]+)\)", ln)
            if m:
                cur = dict(tkr=t, d0=m.group(1), side=1 if m.group(2) == "LONG" else -1,
                           entry=float(m.group(3)), R=float(m.group(6)))
                continue
            m = re.search(r"(\d{4}-\d\d-\d\d)\s+EXIT @ ([\d.]+)\s+([+-][\d.]+)R", ln)
            if m and cur:
                cur.update(d1=m.group(1), r=float(m.group(3)))
                trades.append(cur)
                cur = None
    return trades


def replay(tr, px, rule):
    """Walk the bars from entry to exit under an alternative exit rule. Returns R banked."""
    e, R, s = tr["entry"], tr["R"], tr["side"]
    d0 = datetime.date.fromisoformat(tr["d0"])
    stop = e - s * R
    peak = 0.0
    for d, o, h, l, c in px:
        if d <= d0:
            continue
        fav = (h - e) / R if s == 1 else (e - l) / R      # best point reached that day
        adv = (l - e) / R if s == 1 else (e - h) / R      # worst point reached that day
        # stop first (conservative: assume the adverse extreme came first)
        hit = (l <= stop) if s == 1 else (h >= stop)
        if hit:
            return (stop - e) / R * s
        peak = max(peak, fav)
        if rule["kind"] == "target" and peak >= rule["t"]:
            return rule["t"]
        if rule["kind"] == "trail":
            a, give = rule["a"], rule["give"]
            if peak >= a:
                lvl = np.floor(peak - give)
                stop = e + s * lvl * R
        if rule["kind"] == "atr_chandelier" and peak >= rule["a"]:
            stop = e + s * (peak - rule["give"]) * R
        if rule["kind"] == "timed" and (d - d0).days >= rule["days"]:
            return (c - e) / R * s
    return (px[-1][4] - e) / R * s    # still open: mark to last close


def main():
    trades = parse_trades()
    cache = {}
    print("=" * 96)
    print("EVERY TRADE: how far did it actually run in our favour before it died?")
    print("=" * 96)
    print("%-6s %-5s %-11s %6s %7s %7s   %s" % ("tkr", "side", "entry", "actual", "MFE", "MAE", "verdict"))
    mfes = []
    for t in sorted(trades, key=lambda z: z["d0"]):
        if t["tkr"] not in cache:
            cache[t["tkr"]] = bars(t["tkr"])
            time.sleep(0.3)
        px = cache[t["tkr"]]
        d0, d1 = datetime.date.fromisoformat(t["d0"]), datetime.date.fromisoformat(t["d1"])
        seg = [b for b in px if d0 < b[0] <= d1]
        if not seg:
            continue
        e, R, s = t["entry"], t["R"], t["side"]
        mfe = max(((b[2] - e) / R if s == 1 else (e - b[3]) / R) for b in seg)
        mae = min(((b[3] - e) / R if s == 1 else (e - b[2]) / R) for b in seg)
        mfes.append(mfe)
        if mfe < 0.5:
            v = "never went anywhere"
        elif mfe < 1.0:
            v = "ran <1R then reversed"
        elif mfe < 2.0:
            v = "ran %.1fR - trail NEVER armed, still lost a full -1R" % mfe
        else:
            v = "ran %.1fR and gave back %.1fR to the trail" % (mfe, mfe - t["r"])
        print("%-6s %-5s %-11s %+6.2f %+7.2f %+7.2f   %s"
              % (t["tkr"], "LONG" if s == 1 else "SHORT", t["d0"], t["r"], mfe, mae, v))

    mfes = np.array(mfes)
    print("\nMFE summary over %d trades:" % len(mfes))
    for lo, hi in ((0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 99)):
        n = ((mfes >= lo) & (mfes < hi)).sum()
        print("   ran %4.1f-%4.1fR : %2d trades  (%2.0f%%)" % (lo, hi, n, 100 * n / len(mfes)))
    print("   median MFE %.2fR   mean %.2fR" % (np.median(mfes), mfes.mean()))
    print("   trades that reached +1R but still lost: %d" %
          sum(1 for t, m in zip(sorted(trades, key=lambda z: z["d0"]), mfes) if m >= 1.0 and t["r"] <= 0))

    print("\n" + "=" * 96)
    print("SAME 17 ENTRIES, DIFFERENT EXIT RULES  (entries unchanged - any difference is the exit)")
    print("=" * 96)
    rules = [
        ("LIVE: trail arms at 2R, gives back 2R", {"kind": "trail", "a": 2.0, "give": 2.0}),
        ("trail arms at 1R, gives back 1R",       {"kind": "trail", "a": 1.0, "give": 1.0}),
        ("trail arms at 1R, gives back 0.5R",     {"kind": "atr_chandelier", "a": 1.0, "give": 0.5}),
        ("trail arms at 1.5R, gives back 1R",     {"kind": "atr_chandelier", "a": 1.5, "give": 1.0}),
        ("fixed target 1R",                        {"kind": "target", "t": 1.0}),
        ("fixed target 1.5R",                      {"kind": "target", "t": 1.5}),
        ("fixed target 2R",                        {"kind": "target", "t": 2.0}),
        ("fixed target 3R",                        {"kind": "target", "t": 3.0}),
        ("time exit after 10 days",                {"kind": "timed", "days": 10}),
        ("time exit after 20 days",                {"kind": "timed", "days": 20}),
    ]
    for label, rule in rules:
        rs = []
        for t in trades:
            px = cache.get(t["tkr"])
            if not px:
                continue
            rs.append(replay(t, px, rule))
        rs = np.array(rs)
        wr = 100 * (rs > 0).mean()
        se = rs.std() / np.sqrt(len(rs))
        print("  %-38s expR %+6.3f  (t=%+5.2f)  WR %3.0f%%  total %+6.1fR"
              % (label, rs.mean(), rs.mean() / se if se else 0, wr, rs.sum()))


if __name__ == "__main__":
    main()
