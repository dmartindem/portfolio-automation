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


def fetch(symbols):
    """Return {symbol: (date, close, prev_close)} for the most recent bar."""
    out = {}
    data = yf.download(symbols, period='5d', interval='1d',
                       group_by='ticker', auto_adjust=True,
                       progress=False, threads=True)
    for s in symbols:
        try:
            df = data[s] if len(symbols) > 1 else data
            df = df.dropna(subset=['Close'])
            if len(df) == 0:
                continue
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            out[s] = (df.index[-1].date().isoformat(),
                      round(float(last['Close']), 4),
                      round(float(prev['Close']), 4))
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
        return (last['Date'], round(float(last['Close']), 4), round(float(prev['Close']), 4))
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
            rows = [r for r in csv.DictReader(f) if r['date'] != bar_date]

    for s, (d, close, prev) in sorted(quotes.items()):
        if d != bar_date:
            continue                                # stale symbol, skip
        pct = round((close / prev - 1) * 100, 3) if prev else 0.0
        rows.append({'date': d, 'symbol': s, 'close': close, 'pct_change': pct})

    rows.sort(key=lambda r: (r['date'], r['symbol']))
    os.makedirs(os.path.dirname(PRICES), exist_ok=True)
    with open(PRICES, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['date', 'symbol', 'close', 'pct_change'])
        w.writeheader()
        w.writerows(rows)

    got = sum(1 for r in rows if r['date'] == bar_date)
    missing = sorted(set(symbols) - {r['symbol'] for r in rows if r['date'] == bar_date})
    print(f'{bar_date}: wrote {got}/{len(symbols)} symbols, {len(rows)} total rows')
    if missing:
        print(f'missing: {", ".join(missing)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
