#!/usr/bin/env python3
"""
Append today's closing prices for every portfolio holding to data/prices.csv.

Idempotent: re-running on the same date overwrites that date's rows rather than
duplicating them. Safe to run on weekends/holidays — it just finds no new bar.
"""
import csv
import io
import os
import sys
import urllib.request

import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTFOLIO = os.path.join(ROOT, 'data', 'portfolio.csv')
PRICES = os.path.join(ROOT, 'data', 'prices.csv')

# Symbols that aren't fetchable equity tickers, or need remapping for Yahoo.
SKIP = {'SPAXX**', 'SPAXX'}
REMAP = {
    'KRKNF': 'KRKNF',        # OTC, usually available
    'BRK.B': 'BRK-B',
}
# Benchmarks tracked alongside the holdings.
BENCHMARKS = ['SPY', 'QQQ', 'RSP', 'IWM', 'TLT']


def watchlist():
    """Unique fetchable tickers: every non-option holding, plus benchmarks."""
    syms = set()
    with open(PORTFOLIO) as f:
        for row in csv.DictReader(f):
            if row['kind'] == 'option' or row['symbol'] in SKIP:
                continue
            syms.add(REMAP.get(row['symbol'], row['symbol']))
    return sorted(syms | set(BENCHMARKS))


MOVERS = os.path.join(ROOT, 'data', 'movers.csv')
LOOKBACKS = {'chg_1d': 1, 'chg_1w': 5, 'chg_1m': 21}   # trading bars back


def _changes(closes):
    """% change from 1, 5 and 21 bars back, given closes oldest->newest.
    A lookback longer than the history available is left blank, never guessed."""
    out = {}
    last = closes[-1]
    for key, n in LOOKBACKS.items():
        out[key] = round((last / closes[-1 - n] - 1) * 100, 3) if len(closes) > n and closes[-1 - n] else ''
    return out


def fetch(symbols):
    """Return {symbol: (date, close, prev_close, changes)} for the most recent bar."""
    out = {}
    data = yf.download(symbols, period='3mo', interval='1d',
                       group_by='ticker', auto_adjust=True,
                       progress=False, threads=True)
    for s in symbols:
        try:
            df = data[s] if len(symbols) > 1 else data
            df = df.dropna(subset=['Close'])
            if len(df) == 0:
                continue
            closes = [float(c) for c in df['Close'].tolist()]
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            out[s] = (df.index[-1].date().isoformat(),
                      round(float(last['Close']), 4),
                      round(float(prev['Close']), 4),
                      _changes(closes))
        except Exception as e:                     # noqa: BLE001
            print(f'  ! {s}: {e}', file=sys.stderr)
    return out


def fetch_stooq(symbol):
    """Fallback: Stooq's free CSV endpoint. No key, no rate limit worth worrying about.

    Yahoo breaks every few months; this keeps the series going when it does.
    US tickers are suffixed '.us' and dots become dashes (BRK.B -> brk-b.us).
    """
    slug = symbol.lower().replace('.', '-')
    url = f'https://stooq.com/q/d/l/?s={slug}.us&i=d'
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            text = r.read().decode()
        rows = list(csv.DictReader(io.StringIO(text)))
        rows = [x for x in rows if x.get('Close') not in (None, '', 'N/D')]
        if not rows:
            return None
        last, prev = rows[-1], (rows[-2] if len(rows) > 1 else rows[-1])
        closes = [float(x['Close']) for x in rows[-40:]]
        return (last['Date'], round(float(last['Close']), 4), round(float(prev['Close']), 4),
                _changes(closes))
    except Exception:                              # noqa: BLE001
        return None


def main():
    symbols = watchlist()
    print(f'Fetching {len(symbols)} symbols...')
    quotes = fetch(symbols)

    missing_first = sorted(set(symbols) - set(quotes))
    if missing_first:
        print(f'Yahoo missed {len(missing_first)} — trying Stooq fallback...')
        for s in missing_first:
            got = fetch_stooq(s)
            if got:
                quotes[s] = got

    if not quotes:
        print('No data returned — market closed or upstream unavailable. Exiting cleanly.')
        return 0

    # Latest bar date across the fetch (they should agree on a normal trading day).
    bar_date = max(v[0] for v in quotes.values())

    rows = []
    if os.path.exists(PRICES):
        with open(PRICES) as f:
            keys = {(d, sym) for sym, (d, _c, _p, _ch) in quotes.items()}
            rows = [r for r in csv.DictReader(f)
                    if (r['date'], r['symbol']) not in keys]

    movers = []
    for s, (d, close, prev, ch) in sorted(quotes.items()):
        pct = round((close / prev - 1) * 100, 3) if prev else 0.0
        rows.append({'date': d, 'symbol': s, 'close': close, 'pct_change': pct})
        movers.append({'date': d, 'symbol': s, 'close': close, **ch})

    rows.sort(key=lambda r: (r['date'], r['symbol']))
    os.makedirs(os.path.dirname(PRICES), exist_ok=True)
    with open(PRICES, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['date', 'symbol', 'close', 'pct_change'])
        w.writeheader()
        w.writerows(rows)

    # Snapshot (not a series): each symbol's change over 1 / 5 / 21 trading bars,
    # computed from the history the download already carries. Overwritten each run.
    with open(MOVERS, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['date', 'symbol', 'close', *LOOKBACKS])
        w.writeheader()
        w.writerows(movers)

    got = len(quotes)
    missing = sorted(set(symbols) - set(quotes))
    print(f'wrote {got}/{len(symbols)} symbols, {len(rows)} total rows; latest bar {bar_date}')
    if missing:
        print(f'missing: {", ".join(missing)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
