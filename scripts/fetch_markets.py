#!/usr/bin/env python3
"""Write data/markets.json: the last completed daily close for six reference instruments.

Public market data only. This script must never read portfolio.csv or any holdings file.
Always exits 0 - a failed symbol keeps its last good value, flagged stale.

"Previous close" means the day before today in Chicago time (Daniel's day), so every tile
shows the same day's close whenever the job runs: stocks and WTI their last session on or
before that date, Bitcoin that UTC day. The job runs each morning before the US open; an
evening run produces the same answer, so it can never mix dates on the page.

Guards added 2026-09-16 after delayed evening runs (fired after 00:00 UTC) got data
missing the session that had just closed:
  * explicit start/end instead of Yahoo's range= so the request window always covers today
  * never replace a tile with an OLDER bar than the one already published
"""
import json
import math
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

OUT = "data/markets.json"
NY = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

# Display order on the page: markets row first, then holdings row.
INSTRUMENTS = [
    # id      label        yahoo      unit              decimals  session
    ("btc",  "Bitcoin",   "BTC-USD", "USD",            0,        "crypto"),
    ("spx",  "S&P 500",   "^GSPC",   "index points",   2,        "nyse"),
    ("wti",  "WTI crude", "CL=F",    "USD per barrel", 2,        "cme"),
    ("nvda", "NVDA",      "NVDA",    "USD",            2,        "nyse"),
    ("asts", "ASTS",      "ASTS",    "USD",            2,        "nyse"),
    ("vug",  "VUG",       "VUG",     "USD",            2,        "nyse"),
]

# A bar dated today counts as complete only after its session ends, plus a buffer for Yahoo
# to publish the final print. Crypto's daily bar ends at 00:00 UTC, so a bar dated today
# is always still in progress.
SESSION_END_NY = {"nyse": time(16, 30), "cme": time(17, 30), "crypto": None}


def completed_bars(symbol, session, cutoff):
    now_utc = datetime.now(timezone.utc)
    hist = yf.Ticker(symbol).history(start=(now_utc - timedelta(days=20)).date().isoformat(),
                                     end=(now_utc + timedelta(days=2)).date().isoformat(),
                                     interval="1d", auto_adjust=False)
    closes = hist["Close"].dropna()            # per symbol - never align dates across instruments
    if closes.empty:
        return []
    print(f"{symbol}: raw bars {', '.join(ts.date().isoformat() for ts in closes.index[-3:])}")
    bar_tz = closes.index.tz or timezone.utc   # keep the index tz-aware; don't strip tz first
    bars = [(ts.date(), float(c)) for ts, c in closes.items() if math.isfinite(float(c))]
    today = datetime.now(bar_tz).date()
    end = SESSION_END_NY[session]
    while bars and (bars[-1][0] > today or
                    (bars[-1][0] == today and (end is None or datetime.now(NY).time() < end))):
        bars.pop()                              # still in progress
    return [b for b in bars if b[0] <= cutoff]  # prior day (Chicago) or earlier


def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            return {r["id"]: r for r in json.load(f).get("instruments", [])}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return {}


def main():
    previous = load_previous()
    cutoff = datetime.now(CT).date() - timedelta(days=1)
    print(f"target: closes on or before {cutoff.isoformat()} (yesterday, America/Chicago)")
    instruments, fresh = [], 0
    for iid, label, symbol, unit, decimals, session in INSTRUMENTS:
        base = {"id": iid, "label": label, "symbol": symbol, "unit": unit, "decimals": decimals}
        try:
            bars = completed_bars(symbol, session, cutoff)
            if len(bars) < 2:
                raise ValueError("fewer than two completed bars")
            (_, c_prev), (d_last, c_last) = bars[-2], bars[-1]
            old = previous.get(iid) or {}
            if (old.get("close") is not None and old.get("bar_date")
                    and d_last.isoformat() < old["bar_date"] <= cutoff.isoformat()):
                # Yahoo returned less than we already have: keep the newer published bar.
                instruments.append({**base, **{k: old.get(k) for k in
                                    ("close", "prev_close", "change_pct", "bar_date")}, "stale": False})
                fresh += 1
                print(f"{symbol}: Yahoo's latest is {d_last.isoformat()}, older than published "
                      f"{old['bar_date']} - keeping published")
                continue
            instruments.append({
                **base,
                "close": round(c_last, 4),
                "prev_close": round(c_prev, 4),
                "change_pct": round((c_last / c_prev - 1) * 100, 2),
                "bar_date": d_last.isoformat(),
                "stale": False,
            })
            fresh += 1
            print(f"{symbol}: ok, bar {d_last.isoformat()}")
        except Exception as exc:                # one bad symbol never sinks the rest
            print(f"{symbol}: failed ({type(exc).__name__}), carrying forward")
            old = previous.get(iid) or {}
            instruments.append({
                **base,
                "close": old.get("close"),
                "prev_close": old.get("prev_close"),
                "change_pct": old.get("change_pct"),
                "bar_date": old.get("bar_date"),
                "stale": True,
            })

    doc = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance via yfinance; last completed daily bar per instrument",
        "instruments": instruments,
    }
    # Serialise fully before touching the file, so a NaN can't leave a truncated file behind
    # (the analytics.json NaN bug, START-HERE section 7).
    text = json.dumps(doc, indent=2, allow_nan=False) + "\n"
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, OUT)
    print(f"markets.json: {fresh}/{len(INSTRUMENTS)} fresh")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"fetch_markets: aborted ({type(exc).__name__}); markets.json left unchanged")
    raise SystemExit(0)
