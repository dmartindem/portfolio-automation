# Portfolio automation — taxable brokerage

The deterministic data layer behind the Claude portfolio monitor. Runs free on
GitHub Actions — no server, no API keys, no paid plan.

## Why this repo exists

Two things are not reachable from a Claude session: Yahoo Finance is blocked by
the egress proxy, and Stooq disallows automated fetching in robots.txt. So
anything needing a **multi-year daily returns matrix** — factor models, VaR,
stress replays, risk decomposition — has to run somewhere with open network.
That is this repo. A GitHub runner has it.

The split:

| | Where | What |
|---|---|---|
| **Tier 1** | Claude scheduled tasks | look-through, tax position, drift, concentration, options ladder, calendar, news |
| **Tier 2** | here | factor loadings, risk metrics, VaR/CVaR, stress tests, risk decomposition |

## Keep this repo public — deliberately

Scheduled workflows are unreliable on private repos on the free plan (widely
reported, inconsistently documented; some see cron never fire). Rather than pay
$4/month for Pro to work around it, **nothing sensitive goes in the repo at all.**

`data/portfolio.csv` carries tickers, weights and classification. It does **not**
carry cost basis, share counts, prices paid, or account value. Those live in the
private Claude project. Weights are percentages — they say what the shape of the
book is, never how large it is.

That single constraint makes a public repo safe, which makes free cron work,
which removes the whole problem. Keep it that way: **never commit a file
containing dollar amounts or cost basis**, and never print them in a workflow
log — Actions logs on a public repo are world-readable.

## What runs

| Workflow | Schedule | Output |
|---|---|---|
| `daily-snapshot.yml` | weekdays 22:30 UTC | `data/prices.csv`, `data/concentration.csv` |
| `weekly-analytics.yml` | Sundays 20:00 UTC | `data/analytics.json` |

Sunday 20:00 UTC is three hours before the Claude weekly review at 23:00 UTC, so
the analytics are fresh when it reads them.

- **`scripts/fetch_prices.py`** — closing prices for every priceable holding plus
  SPY / QQQ / RSP / IWM / TLT. Yahoo primary, Stooq fallback. Idempotent per date.
- **`scripts/concentration.py`** — drifts weights by price change, re-blends ETF
  constituents, appends look-through exposure and any threshold breach.
- **`scripts/analytics.py`** — 5 years of daily returns → annualised vol, beta,
  Sharpe, Sortino, tracking error, information ratio, max drawdown and duration,
  historical + Cornish-Fisher VaR, CVaR, up/down capture, skew and kurtosis;
  component contribution to variance (sums to portfolio vol), diversification
  ratio, HHI and effective N; Fama-French 5 + momentum with t-stats and
  significance flags; and replays of 2008, Mar 2020, 2022 and Q4 2018.

## Setup — about 5 minutes

```bash
gh repo create portfolio-automation --public --source=. --push
```

Then **Settings → Actions → General → Workflow permissions → Read and write
permissions**. Without it the jobs can't commit their results.

Test both from the **Actions** tab with *Run workflow* before trusting the cron.

GitHub disables scheduled workflows after 60 days of repo inactivity; these
commit on every successful run, which counts as activity.

## Feeding it back to Claude

Add these to the weekly task's config (`claude/config-weekly.yml`, key
`tier_2_source`):

```
https://raw.githubusercontent.com/<you>/portfolio-automation/main/data/analytics.json
https://raw.githubusercontent.com/<you>/portfolio-automation/main/data/concentration.csv
```

Until that URL resolves, the weekly review prints "Tier 2 unavailable — needs the
GitHub data layer" for those sections rather than approximating them.

## Updating holdings

After each Fidelity export, regenerate `data/portfolio.csv` — tickers, weights and
classification only. Refresh `data/etf_weights.json` quarterly from
`stockanalysis.com/etf/<ticker>/holdings/`; fund composition drifts and the
look-through is only as current as those weights.

## Known limits

- **The return series is a backtest of the current book.** It assumes today's
  weights were held for the whole window. It is not realised past performance, and
  `analytics.json` carries that caveat in a field so the report repeats it.
- **Options and cash are excluded** from the returns matrix; weights are
  renormalised over what can be priced. The 3.3% option sleeve is therefore absent
  from every Tier 2 number.
- **Young names get dropped** — anything with under ~250 daily observations. The
  coverage block names them and reports what fraction of the book survived.
- **Stress replays understate** for a book this young: names without history in a
  2008 or 2020 window contribute zero rather than a factor-implied return.
- **Ken French data lags** to prior month-end, so factor loadings are never
  current to the day. The output states its own `data_through` date.
