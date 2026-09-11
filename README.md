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
| `mirror-reports.yml` | weekdays 13:00 UTC, Mondays 00:30 UTC | `data/reports/*.html`, `data/reports/index.json` |

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

## The pages

GitHub Pages serves three pages from the repo root:

| Page | Reads | What it shows |
|---|---|---|
| `index.html` — Portfolio Overview | `data/analytics.json`, `concentration.csv`, `prices.csv` | Tier 2 risk tiles (Sharpe carries SPY over the same window as a reference), risk decomposition, look-through table, factors, stress replays, last session |
| `daily.html` — Daily Updates | `data/reports/index.json` | the latest Daily Brief in full, earlier ones collapsed by date |
| `weekly.html` — Weekly Updates | `data/reports/index.json` | the latest Weekly Review in full, earlier ones collapsed by date |

### How the reports get here — `scripts/mirror_reports.py`

The Claude scheduled tasks email each report and cannot push to this repo. So
a workflow logs into that mailbox over IMAP, takes every message from the last
three weeks whose subject starts with `Daily Brief` or `Weekly Review`, and
mirrors the HTML body into `data/reports/<kind>-<date>.html`.

Safeguards, because the repo is public:

- **Sender check** — only messages *from* the mailbox's own address are
  accepted. Nobody can publish to the site by emailing it a report.
- **Sanitiser** — the body is reduced to a tag allow-list (headings, paragraphs,
  lists, tables, links with `https` hrefs). Scripts, styles, images, iframes and
  all other attributes are dropped. The pages run a second pass client-side.
- **Privacy guard** — anything shaped like a dollar amount, a share or contract
  count, a cost basis or an account value is replaced with `[redacted]` before
  it is written. A report that needs more than 25 redactions is skipped, not
  published. Option strikes like `$150` survive; `$6k`, `$12,345` and
  `150 shares` do not. The index records how many redactions each report had.
- **No body text in logs** — the job prints counts only.

Two secrets are required (Settings → Secrets and variables → Actions):
`GMAIL_USER` (the mailbox address) and `GMAIL_APP_PASSWORD` (a Google app
password; the account needs 2-Step Verification on). Without them the job
fails with a clear message and the pages say "no reports mirrored yet".

The job is idempotent: re-running it rewrites nothing unless an email changed,
so it never produces churn commits. If the same report was sent twice on one
day, the later send wins.

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
