#!/usr/bin/env python3
"""
Recompute look-through concentration from the latest prices and append one row
to data/concentration.csv. This is the time series the weekly review reads to
detect drift.

Weights drift with price: a position's weight today = baseline_weight x
(price_today / price_baseline), renormalised so the book sums to 100%. Positions
without price data (cash, options) hold their baseline weight.
"""
import csv
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS = os.path.join(ROOT, 'data', 'holdings.csv')
PRICES = os.path.join(ROOT, 'data', 'prices.csv')
ETFW = os.path.join(ROOT, 'data', 'etf_weights.json')
OUT = os.path.join(ROOT, 'data', 'concentration.csv')

MEGA8 = ['NVDA', 'AAPL', 'MSFT', 'GOOG/L', 'AMZN', 'META', 'TSLA', 'AVGO']
SEMIS = ['NVDA', 'AVGO', 'AMD', 'MU', 'AMAT', 'LRCX', 'KLAC', 'INTC', 'MRVL', 'TXN', 'SNDK']
LEVERAGE = {'TQQQ': 3.0}
SMALLCAP = {'SCHA', 'IWM'}


def norm(t):
    return 'GOOG/L' if t in ('GOOG', 'GOOGL') else t


def load_prices():
    """{date: {symbol: close}} plus the sorted list of dates."""
    by_date = defaultdict(dict)
    if not os.path.exists(PRICES):
        return by_date, []
    with open(PRICES) as f:
        for r in csv.DictReader(f):
            by_date[r['date']][r['symbol']] = float(r['close'])
    return by_date, sorted(by_date)


def main():
    holdings = list(csv.DictReader(open(HOLDINGS)))
    etf = json.load(open(ETFW))
    by_date, dates = load_prices()
    if not dates:
        print('No price history yet — run fetch_prices.py first.')
        return 0

    latest, base = dates[-1], dates[0]
    px_now, px_base = by_date[latest], by_date[base]

    # Drift each holding's weight by its price change since the first price date.
    drifted = []
    for h in holdings:
        w = float(h['weight_pct'])
        s = h['symbol']
        if s in px_now and s in px_base and px_base[s]:
            w *= px_now[s] / px_base[s]
        drifted.append({**h, 'w': w})
    total = sum(h['w'] for h in drifted)
    for h in drifted:
        h['w'] = h['w'] / total * 100.0            # renormalise to 100%

    direct, viaetf, optprem = defaultdict(float), defaultdict(float), defaultdict(float)
    cash = options_total = 0.0
    for h in drifted:
        w = h['w']
        if h['kind'] == 'cash':
            cash += w
        elif h['kind'] == 'option':
            optprem[norm(h['underlying'])] += w
            options_total += w
        elif h['symbol'] in SMALLCAP:
            pass                                    # diversified, no mega-cap overlap
        elif h['symbol'] in etf:
            eff = w * LEVERAGE.get(h['symbol'], 1.0)
            for t, fw in etf[h['symbol']].items():
                viaetf[norm(t)] += eff * fw / 100.0
        else:
            direct[norm(h['symbol'])] += w

    def allin(t):
        return direct[t] + viaetf[t] + optprem.get(t, 0.0)

    row = {
        'date': latest,
        'nvda_allin': round(allin('NVDA'), 3),
        'asts_allin': round(allin('ASTS'), 3),
        'top2': round(allin('NVDA') + allin('ASTS'), 3),
        'mega8': round(sum(direct[t] + viaetf[t] for t in MEGA8), 3),
        'semis': round(sum(direct[t] + viaetf[t] for t in SEMIS), 3),
        'options_pct': round(options_total, 3),
        'cash_pct': round(cash, 3),
    }

    # Threshold checks — mirrors claude/portfolio-policy.md
    breaches = []
    for k, limit, label in [('nvda_allin', 15.0, 'NVDA >15%'),
                            ('asts_allin', 15.0, 'ASTS >15%'),
                            ('top2', 32.0, 'top-2 >32%'),
                            ('mega8', 55.0, 'mega-cap 8 >55%'),
                            ('semis', 27.0, 'semis >27%'),
                            ('options_pct', 5.0, 'options >5%')]:
        if row[k] > limit:
            breaches.append(label)
    if row['cash_pct'] < 1.0:
        breaches.append('cash <1%')
    if row['cash_pct'] > 10.0:
        breaches.append('cash >10%')
    row['breaches'] = '; '.join(breaches)

    rows = []
    if os.path.exists(OUT):
        rows = [r for r in csv.DictReader(open(OUT)) if r['date'] != latest]
    rows.append(row)
    rows.sort(key=lambda r: r['date'])
    with open(OUT, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        w.writeheader()
        w.writerows(rows)

    print(f'{latest}  NVDA {row["nvda_allin"]}%  ASTS {row["asts_allin"]}%  '
          f'mega8 {row["mega8"]}%  semis {row["semis"]}%')
    print(f'breaches: {row["breaches"] or "none"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
