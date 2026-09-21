"""Risk analytics: volatility, drawdown, risk-adjusted ratios, benchmark relationship,
correlation, risk contribution and concentration.

Pure functions over pandas objects. Every one returns None or an empty object rather
than raising when there is not enough data, so the dashboard degrades instead of breaking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

TRADING_DAYS = 252
MIN_OBS = 20  # below this, statistics are noise and we report nothing


@dataclass(frozen=True)
class Drawdown:
    max_drawdown: float          # negative, e.g. -0.32
    peak: date | None
    trough: date | None
    recovered: date | None       # None if still under water
    days_under_water: int


@dataclass
class RiskStats:
    n_obs: int
    volatility: float | None = None        # annualised
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    drawdown: Drawdown | None = None
    var_95: float | None = None            # daily historical VaR, negative
    cvar_95: float | None = None
    beta: float | None = None
    alpha: float | None = None             # annualised
    tracking_error: float | None = None
    information_ratio: float | None = None
    up_capture: float | None = None
    down_capture: float | None = None
    benchmark: str | None = None
    risk_free: float = 0.0

    @property
    def reliable(self) -> bool:
        return self.n_obs >= TRADING_DAYS // 2


def annualised_volatility(returns: pd.Series, periods: int = TRADING_DAYS) -> float | None:
    r = _clean(returns)
    return float(r.std(ddof=1) * np.sqrt(periods)) if len(r) >= MIN_OBS else None


def sharpe_ratio(returns: pd.Series, risk_free: float = 0.0, periods: int = TRADING_DAYS) -> float | None:
    r = _clean(returns)
    if len(r) < MIN_OBS:
        return None
    excess = r - risk_free / periods
    sd = excess.std(ddof=1)
    return float(excess.mean() / sd * np.sqrt(periods)) if sd > 0 else None


def sortino_ratio(returns: pd.Series, risk_free: float = 0.0, periods: int = TRADING_DAYS) -> float | None:
    r = _clean(returns)
    if len(r) < MIN_OBS:
        return None
    excess = r - risk_free / periods
    downside = excess[excess < 0]
    dd = np.sqrt((downside ** 2).sum() / len(excess)) if len(downside) else 0.0
    return float(excess.mean() / dd * np.sqrt(periods)) if dd > 0 else None


def drawdown_series(level: pd.Series) -> pd.Series:
    lv = level.astype(float).dropna()
    return (lv / lv.cummax() - 1.0) if len(lv) else pd.Series(dtype=float)


def max_drawdown(level: pd.Series) -> Drawdown | None:
    """Deepest peak-to-trough fall of a level series, with dates and recovery."""
    lv = level.astype(float).dropna()
    if len(lv) < 2:
        return None
    dd = lv / lv.cummax() - 1.0
    trough_i = dd.idxmin()
    worst = float(dd.loc[trough_i])
    peak_i = lv.loc[:trough_i].idxmax()
    after = lv.loc[trough_i:]
    recovered = after[after >= lv.loc[peak_i]]
    rec_i = recovered.index[0] if len(recovered) else None
    end = rec_i if rec_i is not None else lv.index[-1]
    return Drawdown(max_drawdown=worst, peak=_d(peak_i), trough=_d(trough_i),
                    recovered=_d(rec_i) if rec_i is not None else None,
                    days_under_water=int((end - peak_i).days))


def historical_var(returns: pd.Series, level: float = 0.95) -> tuple[float | None, float | None]:
    """Daily historical VaR and conditional VaR at `level`, both negative numbers."""
    r = _clean(returns)
    if len(r) < MIN_OBS:
        return None, None
    var = float(np.quantile(r, 1 - level))
    tail = r[r <= var]
    return var, (float(tail.mean()) if len(tail) else var)


def beta_alpha(portfolio: pd.Series, benchmark: pd.Series, risk_free: float = 0.0,
               periods: int = TRADING_DAYS) -> tuple[float | None, float | None]:
    p, b = _align(portfolio, benchmark)
    if len(p) < MIN_OBS:
        return None, None
    rf = risk_free / periods
    pe, be = p - rf, b - rf
    var_b = be.var(ddof=1)
    if var_b <= 0:
        return None, None
    beta = float(pe.cov(be) / var_b)
    alpha_daily = float(pe.mean() - beta * be.mean())
    return beta, float((1 + alpha_daily) ** periods - 1)


def tracking_stats(portfolio: pd.Series, benchmark: pd.Series, periods: int = TRADING_DAYS):
    """Tracking error (annualised) and information ratio."""
    p, b = _align(portfolio, benchmark)
    if len(p) < MIN_OBS:
        return None, None
    diff = p - b
    te = float(diff.std(ddof=1) * np.sqrt(periods))
    return te, (float(diff.mean() * periods / te) if te > 0 else None)


def capture_ratios(portfolio: pd.Series, benchmark: pd.Series) -> tuple[float | None, float | None]:
    """How much of the benchmark's up and down moves the portfolio captured.

    Geometric convention (as used by Morningstar): the compounded return over the
    benchmark's up periods, divided by the benchmark's own compounded return over
    those periods. Because it compounds, a portfolio that doubles every up move
    scores well above 2; read these as "more or less than the benchmark", not as
    a precise multiple.
    """
    p, b = _align(portfolio, benchmark)
    if len(p) < MIN_OBS:
        return None, None
    out = []
    for mask in (b > 0, b < 0):
        if mask.sum() < MIN_OBS // 2:
            out.append(None)
            continue
        pb = float((1 + b[mask]).prod() - 1)
        pp = float((1 + p[mask]).prod() - 1)
        out.append(pp / pb if pb else None)
    return out[0], out[1]


def summarise(portfolio_returns: pd.Series, level: pd.Series | None = None,
              benchmark_returns: pd.Series | None = None, benchmark_name: str | None = None,
              risk_free: float = 0.0) -> RiskStats:
    r = _clean(portfolio_returns)
    s = RiskStats(n_obs=len(r), risk_free=risk_free, benchmark=benchmark_name)
    if not len(r):
        return s
    s.volatility = annualised_volatility(r)
    s.sharpe = sharpe_ratio(r, risk_free)
    s.sortino = sortino_ratio(r, risk_free)
    s.var_95, s.cvar_95 = historical_var(r)
    lv = level if level is not None and len(level) else (1 + r).cumprod()
    s.drawdown = max_drawdown(lv)
    s.max_drawdown = s.drawdown.max_drawdown if s.drawdown else None
    if benchmark_returns is not None and len(benchmark_returns):
        s.beta, s.alpha = beta_alpha(r, benchmark_returns, risk_free)
        s.tracking_error, s.information_ratio = tracking_stats(r, benchmark_returns)
        s.up_capture, s.down_capture = capture_ratios(r, benchmark_returns)
    return s


def correlation_matrix(returns: pd.DataFrame, min_obs: int = MIN_OBS) -> pd.DataFrame:
    """Pairwise correlations, dropping columns with too little data."""
    if returns is None or returns.empty:
        return pd.DataFrame()
    keep = [c for c in returns.columns if returns[c].notna().sum() >= min_obs]
    return returns[keep].corr() if keep else pd.DataFrame()


def risk_contribution(weights: pd.Series, returns: pd.DataFrame,
                      periods: int = TRADING_DAYS) -> pd.DataFrame:
    """Each holding's share of portfolio volatility.

    Uses marginal contribution to risk: RC_i = w_i * (Sigma w)_i / sigma_p, which sums
    to the portfolio volatility. A holding can carry more risk than its weight suggests
    when it is volatile or correlated with the rest.
    """
    if returns is None or returns.empty or weights is None or weights.empty:
        return pd.DataFrame()
    cols = [c for c in returns.columns if c in weights.index and returns[c].notna().sum() >= MIN_OBS]
    if len(cols) < 2:
        return pd.DataFrame()
    r = returns[cols].dropna()
    w = weights[cols].astype(float)
    if w.sum() <= 0:
        return pd.DataFrame()
    w = w / w.sum()
    cov = r.cov().to_numpy() * periods
    wv = w.to_numpy()
    var_p = float(wv @ cov @ wv)
    if var_p <= 0:
        return pd.DataFrame()
    vol_p = np.sqrt(var_p)
    mcr = (cov @ wv) / vol_p
    rc = wv * mcr
    return pd.DataFrame({"weight": wv, "volatility": np.sqrt(np.diag(cov)),
                         "marginal": mcr, "contribution": rc,
                         "pct_of_risk": rc / vol_p}, index=cols).sort_values("pct_of_risk", ascending=False)


@dataclass(frozen=True)
class Concentration:
    hhi: float                  # Herfindahl index, 1 = one holding
    effective_holdings: float   # 1 / HHI
    top1: float
    top3: float
    top5: float
    n: int


def concentration(weights: pd.Series) -> Concentration | None:
    w = weights.astype(float).dropna()
    w = w[w > 0]
    if w.empty:
        return None
    w = w / w.sum()
    s = w.sort_values(ascending=False)
    hhi = float((w ** 2).sum())
    return Concentration(hhi=hhi, effective_holdings=(1 / hhi if hhi else 0.0),
                         top1=float(s.iloc[:1].sum()), top3=float(s.iloc[:3].sum()),
                         top5=float(s.iloc[:5].sum()), n=int(len(w)))


def _clean(s: pd.Series) -> pd.Series:
    if s is None or not len(s):
        return pd.Series(dtype=float)
    return s.astype(float).replace([np.inf, -np.inf], np.nan).dropna()


def _align(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    df = pd.concat([_clean(a).rename("a"), _clean(b).rename("b")], axis=1).dropna()
    return df["a"], df["b"]


def _d(ts) -> date | None:
    return ts.date() if hasattr(ts, "date") else ts
