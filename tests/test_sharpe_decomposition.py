"""Synthetic-data checks for scripts/sharpe_decomposition.py. Runs on the GitHub runner
before analytics.py; a failure turns the run red so broken maths never publishes.
No network, no real portfolio data. Plain asserts - no pytest needed."""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from sharpe_decomposition import (DecompositionError, compute,  # noqa: E402
                                  portfolio_sharpe_from_series, sharpe_decomposition)

RF = 0.035 / 252


def synthetic(seed=7, t=750, n=8, positive=True):
    rng = np.random.default_rng(seed)
    a = rng.normal(size=(n, n)) * 0.01
    cov = a @ a.T + np.eye(n) * 1e-5
    drift = rng.uniform(0.0002, 0.0015, n) if positive else rng.uniform(-0.002, -0.0005, n)
    drift[1] = -0.0006                                   # always include a loser
    r = rng.multivariate_normal(drift, cov, size=t)
    cols = [f"T{i}" for i in range(n)]
    w = rng.uniform(0.02, 0.3, n)
    return pd.DataFrame(r, columns=cols), pd.Series(w / w.sum(), index=cols)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def main():
    rets, w = synthetic()
    c = compute(rets, w, RF)

    check("contributions sum to Sharpe", abs(c["contribution"].sum() - c["sharpe"]) < 1e-12)
    check("risk shares sum to 1", abs(c["risk_share"].sum() - 1) < 1e-12)
    check("return shares sum to 1", abs(c["return_share"].sum() - 1) < 1e-12)
    check("covariance path == return-series path (ddof=1)",
          abs(portfolio_sharpe_from_series(rets, w, RF) - c["sharpe"]) < 1e-12)
    c0 = compute(rets, w, RF, ddof=0)
    check("covariance path == return-series path (ddof=0)",
          abs(portfolio_sharpe_from_series(rets, w, RF, ddof=0) - c0["sharpe"]) < 1e-12)

    # Marginal: move eps of weight into i, funded pro rata from the whole book.
    eps, worst = 1e-6, 0.0
    for i, t in enumerate(rets.columns):
        e = pd.Series(0.0, index=w.index); e[t] = 1.0
        s2 = compute(rets, (1 - eps) * w + eps * e, RF)["sharpe"]
        worst = max(worst, abs((s2 - c["sharpe"]) / eps - c["marginal"][i]))
    check(f"marginal matches finite difference (worst {worst:.1e})", worst < 1e-4)

    check("weighted marginals sum to 0 (scale invariance)", abs((w.to_numpy() * c["marginal"]).sum()) < 1e-10)
    check("marginal > 0 exactly when return share > risk share",
          all((m > 0) == (er > rs) for m, er, rs in zip(c["marginal"], c["return_share"], c["risk_share"])))
    check("a losing position has a negative contribution", c["contribution"][1] < 0)

    block = sharpe_decomposition(rets, w, RF, expected_sharpe=c["sharpe"])
    check("block serialises with allow_nan=False", bool(json.dumps(block, allow_nan=False)))
    check("block Sharpe matches", abs(block["sharpe"] - c["sharpe"]) < 1e-6)
    check("rounded contributions still sum within 1e-4",
          abs(sum(p["sharpe_contribution"] for p in block["positions"]) - block["sharpe"]) < 1e-4)
    check("positions sorted by contribution, descending",
          [p["sharpe_contribution"] for p in block["positions"]]
          == sorted((p["sharpe_contribution"] for p in block["positions"]), reverse=True))
    check("per-pp marginal is marginal x 0.01",
          all(abs(p["marginal_sharpe_per_pp"] - c["marginal"][rets.columns.get_loc(p["ticker"])] * 0.01) < 1e-6
              for p in block["positions"]))

    rneg, wneg = synthetic(seed=3, positive=False)
    bneg = sharpe_decomposition(rneg, wneg, RF)
    check("negative-excess window: return shares suppressed",
          bneg["return_shares_meaningful"] is False and all(p["return_share"] is None for p in bneg["positions"]))
    check("negative-excess window: contributions still sum to Sharpe",
          abs(sum(p["sharpe_contribution"] for p in bneg["positions"]) - bneg["sharpe"]) < 1e-4)

    def raises(fn):
        try:
            fn()
        except DecompositionError:
            return True
        return False

    bad = rets.copy(); bad.iloc[5, 2] = np.nan
    check("rejects NaN returns", raises(lambda: compute(bad, w, RF)))
    check("rejects weights not summing to 1", raises(lambda: compute(rets, w * 0.92, RF)))
    check("rejects a missing weight", raises(lambda: compute(rets, w.drop("T3"), RF)))
    check("rejects an rf series", raises(lambda: compute(rets, w, np.full(len(rets), RF))))
    check("rejects a Sharpe that doesn't match analytics.py",
          raises(lambda: sharpe_decomposition(rets, w, RF, expected_sharpe=c["sharpe"] + 0.01)))
    print("all Sharpe decomposition checks passed")


if __name__ == "__main__":
    main()
