#!/usr/bin/env python3
"""
Tier 2 analytics: everything that needs a multi-year daily returns matrix.

Not reachable from a Claude session (Yahoo blocked by the egress proxy, Stooq
disallows automated fetching), so this runs on the GitHub Actions runner and
publishes data/analytics.json for the weekly review to read.

Publishes DERIVED METRICS ONLY. No cost basis, no share counts, no dollar
amounts ever enter this repo — that is what lets it stay public, which is what
makes scheduled workflows work on the free plan.
"""
import json, os, sys, csv, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTFOLIO = os.path.join(ROOT, 'data', 'portfolio.csv')
OUT = os.path.join(ROOT, 'data', 'analytics.json')

BENCH = 'SPY'
EXTRA = ['SPY', 'QQQ', 'RSP']
YEARS = 5
TRADING_DAYS = 252
MIN_OBS = 250                      # ~1y; below this a ticker is dropped
RF_ANNUAL = 0.043                  # 3M T-bill approximation; refreshed manually

SCENARIOS = {
    'gfc_2008':   ('2007-10-09', '2009-03-09', '2008 GFC'),
    'covid_2020': ('2020-02-19', '2020-03-23', 'Mar 2020'),
    'rates_2022': ('2022-01-03', '2022-10-12', '2022'),
    'q4_2018':    ('2018-10-01', '2018-12-24', 'Q4 2018'),
}


def load_weights():
    """ticker -> weight%. Options and cash are excluded: no return series."""
    w = {}
    with open(PORTFOLIO) as f:
        for r in csv.DictReader(f):
            if r['kind'] in ('option', 'cash'):
                continue
            w[r['symbol'].replace('.', '-')] = float(r['weight_pct'])
    return w


def fetch(tickers):
    import yfinance as yf
    df = yf.download(tickers, period=f'{YEARS}y', interval='1d',
                     auto_adjust=True, progress=False, threads=True)
    close = df['Close'] if isinstance(df.columns, pd.MultiIndex) else df[['Close']]
    if not isinstance(df.columns, pd.MultiIndex):
        close.columns = tickers[:1]
    return close.dropna(how='all')


def metrics(rp, rm, rf_daily):
    """Risk metrics for a portfolio return series against a benchmark."""
    ex = rp - rf_daily
    dn = rp[rp < 0]
    active = rp - rm
    up, down = rm > 0, rm < 0
    var95, var99 = np.percentile(rp, 5), np.percentile(rp, 1)
    s, k = float(rp.skew()), float(rp.kurtosis())
    z = -1.645
    zcf = z + (z**2 - 1) * s / 6 + (z**3 - 3*z) * k / 24 - (2*z**3 - 5*z) * s**2 / 36
    cum = (1 + rp).cumprod()
    dd = cum / cum.cummax() - 1
    # longest stretch below a previous peak
    under = (dd < 0).astype(int)
    runs, cur = [], 0
    for v in under:
        cur = cur + 1 if v else 0
        runs.append(cur)

    return {
        'annualised_volatility': float(rp.std() * np.sqrt(TRADING_DAYS)),
        'annualised_return': float((1 + rp.mean())**TRADING_DAYS - 1),
        'beta': float(np.cov(rp, rm)[0][1] / np.var(rm)),
        'sharpe': float(ex.mean() / rp.std() * np.sqrt(TRADING_DAYS)),
        'sortino': float(ex.mean() / dn.std() * np.sqrt(TRADING_DAYS)) if len(dn) else None,
        'tracking_error': float(active.std() * np.sqrt(TRADING_DAYS)),
        'information_ratio': float(active.mean() / active.std() * np.sqrt(TRADING_DAYS)),
        'max_drawdown': float(dd.min()),
        'max_drawdown_days': int(max(runs)) if runs else 0,
        'var_95_1d': float(var95), 'var_99_1d': float(var99),
        'var_95_cornish_fisher': float(rp.mean() + zcf * rp.std()),
        'cvar_95_1d': float(rp[rp <= var95].mean()),
        'downside_deviation': float(dn.std() * np.sqrt(TRADING_DAYS)) if len(dn) else None,
        'skew': s, 'excess_kurtosis': k,
        'up_capture': float(rp[up].mean() / rm[up].mean()) if up.any() else None,
        'down_capture': float(rp[down].mean() / rm[down].mean()) if down.any() else None,
    }


def decompose(rets, w):
    """Component contribution to portfolio variance. CCR sums to portfolio vol."""
    cov = rets.cov().values * TRADING_DAYS
    wv = w.values
    var = float(wv @ cov @ wv)
    vol = np.sqrt(var)
    mcr = (cov @ wv) / vol                       # marginal contribution
    ccr = wv * mcr                               # component contribution
    share = ccr / vol
    ind = rets.std().values * np.sqrt(TRADING_DAYS)
    return {
        'portfolio_volatility': vol,
        'diversification_ratio': float((wv @ ind) / vol),
        'hhi': float(np.sum(wv**2)),
        'effective_n': float(1 / np.sum(wv**2)),
        'contributions': sorted(
            [{'ticker': t, 'weight_pct': round(float(wv[i]) * 100, 2),
              'risk_share_pct': round(float(share[i]) * 100, 2),
              'flag': bool(share[i] * 100 > 20.0)}
             for i, t in enumerate(rets.columns)],
            key=lambda d: -d['risk_share_pct']),
    }


def factors(rp):
    """Fama-French 5 + momentum. Ken French data lags to prior month-end."""
    try:
        from pandas_datareader.famafrench import FamaFrenchReader
        ff = FamaFrenchReader('F-F_Research_Data_5_Factors_2x3_daily',
                              start=rp.index[0]).read()[0] / 100
        mom = FamaFrenchReader('F-F_Momentum_Factor_daily',
                               start=rp.index[0]).read()[0] / 100
        X = ff.join(mom, how='inner')
        X.columns = [c.strip() for c in X.columns]
        def _flat(ix):
            if hasattr(ix, 'to_timestamp'):      # Ken French returns a PeriodIndex
                ix = ix.to_timestamp()
            ix = pd.to_datetime(ix)
            if getattr(ix, 'tz', None) is not None:
                ix = ix.tz_localize(None)
            return ix.normalize()
        X.index = _flat(X.index)
        rp2 = rp.copy(); rp2.index = _flat(rp2.index)
        d = X.join(rp2.rename('rp'), how='inner').dropna()
        if len(d) < MIN_OBS:
            return {'available': False, 'reason': f'only {len(d)} overlapping days'}
        y = (d['rp'] - d['RF']).values
        names = ['Mkt-RF', 'SMB', 'HML', 'RMW', 'CMA', 'Mom']
        A = np.column_stack([np.ones(len(d))] + [d[n].values for n in names])
        beta, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = y - A @ beta
        dof = len(d) - A.shape[1]
        se = np.sqrt(np.diag(np.linalg.pinv(A.T @ A)) * (resid @ resid) / dof)
        r2 = 1 - (resid @ resid) / np.sum((y - y.mean())**2)
        return {
            'available': True, 'observations': int(len(d)),
            'data_through': str(d.index[-1].date()),
            'note': 'Ken French updates monthly; loadings run through prior month-end.',
            'r_squared': float(r2),
            'annualised_alpha': float((1 + beta[0])**TRADING_DAYS - 1),
            'loadings': [{'factor': n, 'beta': float(beta[i+1]),
                          't_stat': float(beta[i+1] / se[i+1]),
                          'significant_95': bool(abs(beta[i+1] / se[i+1]) > 1.96)}
                         for i, n in enumerate(names)],
        }
    except Exception as e:                        # noqa: BLE001
        return {'available': False, 'reason': str(e)[:200]}


def main():
    w0 = load_weights()
    tickers = sorted(set(w0) | set(EXTRA))
    print(f'Fetching {len(tickers)} tickers, {YEARS}y daily...')
    px = fetch(tickers)

    ok = [t for t in w0 if t in px.columns and px[t].notna().sum() >= MIN_OBS]
    dropped = sorted(set(w0) - set(ok))
    w = pd.Series({t: w0[t] for t in ok})
    w = w / w.sum()                               # renormalise over what we can price
    print(f'Usable: {len(ok)}/{len(w0)}  covering {sum(w0[t] for t in ok):.1f}% of book')
    if dropped:
        print(f'Dropped (insufficient history): {", ".join(dropped)}')

    rets = px[ok].pct_change().dropna(how='all').fillna(0.0)
    rp = (rets * w).sum(axis=1)
    rm = px[BENCH].pct_change().reindex(rp.index).fillna(0.0)
    rf_daily = RF_ANNUAL / TRADING_DAYS

    out = {
        'generated_utc': pd.Timestamp.utcnow().isoformat(),
        'window': {'start': str(rp.index[0].date()), 'end': str(rp.index[-1].date()),
                   'observations': int(len(rp))},
        'coverage': {'tickers_used': len(ok), 'tickers_requested': len(w0),
                     'book_pct_covered': round(sum(w0[t] for t in ok), 2),
                     'dropped': dropped},
        'caveat': ('BACKTEST OF THE CURRENT BOOK. This series assumes today\'s weights were '
                   'held for the whole window. It is not realised past performance. Options '
                   'and cash are excluded and weights renormalised over priceable holdings.'),
        'risk_metrics': metrics(rp, rm, rf_daily),
        'benchmarks': {b: metrics(px[b].pct_change().reindex(rp.index).fillna(0.0),
                                  rm, rf_daily) for b in EXTRA if b in px.columns},
        'risk_decomposition': decompose(rets, w),
        'factor_analysis': factors(rp),
        'stress_tests': {},
    }

    for key, (a, b, label) in SCENARIOS.items():
        seg = rp.loc[(rp.index >= a) & (rp.index <= b)]
        segm = rm.loc[(rm.index >= a) & (rm.index <= b)]
        cover = len([t for t in ok if px[t].loc[a:b].notna().sum() > 5])
        out['stress_tests'][key] = ({
            'label': label, 'available': False,
            'reason': 'window predates the available history'
        } if len(seg) < 5 else {
            'label': label, 'available': True,
            'portfolio_return': float((1 + seg).prod() - 1),
            'benchmark_return': float((1 + segm).prod() - 1),
            'tickers_with_history': cover, 'of_total': len(ok),
            'note': 'Names without history in this window contribute zero, so the '
                    'drawdown is understated for a book this young.',
        })

    shock_beta = out['risk_metrics']['beta']
    out['shocks'] = {'market_minus_10pct': round(-10 * shock_beta, 2),
                     'method': 'beta-implied, first order only'}

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, 'w'), indent=1, default=str)

    m = out['risk_metrics']
    print(f'\nvol {m["annualised_volatility"]:.1%}  beta {m["beta"]:.2f}  '
          f'sharpe {m["sharpe"]:.2f}  maxDD {m["max_drawdown"]:.1%}')
    print(f'VaR95 {m["var_95_1d"]:.2%}  CVaR95 {m["cvar_95_1d"]:.2%}')
    top = out['risk_decomposition']['contributions'][:5]
    print('top risk: ' + ', '.join(f'{c["ticker"]} {c["risk_share_pct"]}%' for c in top))
    print(f'factors: {"ok" if out["factor_analysis"]["available"] else out["factor_analysis"]["reason"]}')
    print(f'-> {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
