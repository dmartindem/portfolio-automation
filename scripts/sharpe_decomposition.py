"""Additive decomposition of the portfolio Sharpe ratio into per-position contributions.

Runs on the GitHub Actions runner, called from analytics.py once per risk window.
Pure function: no I/O, no network, no dollar figures - ratios and shares only.

Definitions (annualised; r = periodic simple returns, rf = per-period risk-free rate,
w = constant weights summing to 1, V = annualised covariance of r, k = periods per year):

    mu_i     = mean(r_i - rf) * k                  excess return of position i
    vol      = sqrt(w' V w)                        portfolio volatility
    S        = (w . mu) / vol                      portfolio Sharpe
    C_i      = w_i * mu_i / vol                    Sharpe contribution    -> sum(C) == S exactly
    RS_i     = w_i * (V w)_i / (w' V w)            share of variance      -> sum(RS) == 1
    ER_i     = w_i * mu_i / (w . mu)               share of excess return -> sum(ER) == 1
    dS/dw_i  = (mu_i - S * (V w)_i / vol) / vol    change in S per unit of weight moved into i,
                                                   funded pro rata from the whole book

S is scale-invariant in w, so w . grad(S) == 0 and pro-rata funding drops out of dS/dw_i.
When the portfolio excess return is positive, dS/dw_i > 0 exactly when ER_i > RS_i.
"""
import math

import numpy as np
import pandas as pd


class DecompositionError(ValueError):
    """Inputs don't satisfy the conditions under which the decomposition is exact."""


def compute(returns, weights, rf_per_period, periods_per_year=252, ddof=1):
    """Unrounded arrays. Raises DecompositionError on any input the identity can't hold for."""
    if not isinstance(rf_per_period, (int, float)) or not math.isfinite(rf_per_period):
        raise DecompositionError("rf_per_period must be a single finite number")
    if returns.shape[1] < 2:
        raise DecompositionError("need at least two positions")
    if returns.isna().to_numpy().any():
        raise DecompositionError("returns contain NaN - clean them exactly as analytics.py does first")
    w = weights.reindex(returns.columns)
    if w.isna().any():
        raise DecompositionError("no weight for: " + ", ".join(map(str, w.index[w.isna()])))
    if abs(float(w.sum()) - 1.0) > 1e-9:
        raise DecompositionError(f"weights sum to {float(w.sum()):.9f}, not 1")

    r = returns.to_numpy(dtype=float)
    wv = w.to_numpy(dtype=float)
    k = periods_per_year

    mu = (r - rf_per_period).mean(axis=0) * k
    cov = np.cov(r, rowvar=False, ddof=ddof) * k
    var_p = float(wv @ cov @ wv)
    if not var_p > 0:
        raise DecompositionError("portfolio variance is not positive")
    vol = math.sqrt(var_p)
    mu_p = float(wv @ mu)
    sharpe = mu_p / vol
    v_w = cov @ wv

    return {
        "tickers": list(returns.columns),
        "weights": wv,
        "mu": mu,
        "vol": vol,
        "mu_p": mu_p,
        "sharpe": sharpe,
        "contribution": wv * mu / vol,
        "risk_share": wv * v_w / var_p,
        "return_share": (wv * mu / mu_p) if mu_p > 0 else None,
        "marginal": (mu - sharpe * v_w / vol) / vol,
        "observations": r.shape[0],
    }


def portfolio_sharpe_from_series(returns, weights, rf_per_period, periods_per_year=252, ddof=1):
    """The same Sharpe computed the long way, from the portfolio return series."""
    rp = returns.to_numpy(dtype=float) @ weights.reindex(returns.columns).to_numpy(dtype=float)
    return float((rp - rf_per_period).mean() * periods_per_year
                 / (rp.std(ddof=ddof) * math.sqrt(periods_per_year)))


def sharpe_decomposition(returns, weights, rf_per_period, periods_per_year=252, ddof=1,
                         expected_sharpe=None, tol=1e-6):
    """JSON-ready block for analytics.json. Raises DecompositionError if any check fails."""
    c = compute(returns, weights, rf_per_period, periods_per_year, ddof)

    residual = abs(float(c["contribution"].sum()) - c["sharpe"])
    if residual > tol:
        raise DecompositionError(f"contributions don't sum to the Sharpe (residual {residual:.2e})")
    if abs(float(c["risk_share"].sum()) - 1.0) > tol:
        raise DecompositionError("risk shares don't sum to 1")
    series = portfolio_sharpe_from_series(returns, weights, rf_per_period, periods_per_year, ddof)
    if abs(series - c["sharpe"]) > tol:
        raise DecompositionError("covariance path and return-series path disagree - check ddof")
    if expected_sharpe is not None and abs(float(expected_sharpe) - c["sharpe"]) > tol:
        raise DecompositionError(
            f"Sharpe {c['sharpe']:.6f} != analytics.py's {float(expected_sharpe):.6f} - "
            "the definitions differ (returns, weights, rf, ddof or annualisation)")

    def rnd(x, dp=6):
        return None if x is None else round(float(x), dp)

    positions = []
    for i, ticker in enumerate(c["tickers"]):
        positions.append({
            "ticker": ticker,
            "weight": rnd(c["weights"][i]),
            "excess_return_annual": rnd(c["mu"][i]),
            "sharpe_contribution": rnd(c["contribution"][i]),
            "return_share": None if c["return_share"] is None else rnd(c["return_share"][i]),
            "risk_share": rnd(c["risk_share"][i]),
            "marginal_sharpe_per_pp": rnd(c["marginal"][i] * 0.01),
        })
    positions.sort(key=lambda p: p["sharpe_contribution"], reverse=True)

    return {
        "available": True,
        "method": "additive: weight x annualised excess return / portfolio volatility",
        "observations": c["observations"],
        "sharpe": rnd(c["sharpe"]),
        "excess_return_annual": rnd(c["mu_p"]),
        "volatility_annual": rnd(c["vol"]),
        "identity_residual": residual,
        "return_shares_meaningful": c["return_share"] is not None,
        "positions": positions,
    }
