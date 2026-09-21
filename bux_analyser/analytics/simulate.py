"""Simulation: possible futures, stress tests and what-if allocations.

Nothing here predicts anything. A simulation resamples the past to show a range of
outcomes *conditional on stated assumptions*, and every result carries those
assumptions with it so a chart can never be read as a forecast.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS = 252
PERCENTILES = (5, 25, 50, 75, 95)


@dataclass(frozen=True)
class Assumptions:
    """Everything the caller chose, recorded so the output can state them."""
    horizon_years: float
    paths: int
    block_days: int
    drift: str                       # "historical" | "zero" | "fixed"
    annual_drift: float | None       # the rate used when drift is not "historical"
    monthly_contribution: float
    history_days: int
    seed: int | None

    def describe(self) -> str:
        if self.drift == "historical":
            base = "expected return taken from this portfolio's own history"
        elif self.drift == "zero":
            base = "expected return set to zero"
        else:
            base = f"expected return set to {self.annual_drift:.1%} a year"
        return (f"{self.paths:,} paths over {self.horizon_years:g} years, resampling "
                f"{self.history_days} days of history in {self.block_days}-day blocks, "
                f"{base}"
                + (f", adding €{self.monthly_contribution:,.0f} a month" if self.monthly_contribution else ""))


@dataclass
class Simulation:
    assumptions: Assumptions
    start_value: float
    terminal: np.ndarray                                  # value at the horizon, one per path
    percentile_paths: pd.DataFrame = field(default_factory=pd.DataFrame)
    contributed: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def invested(self) -> float:
        return self.start_value + self.contributed

    def percentile(self, p: float) -> float:
        return float(np.percentile(self.terminal, p))

    @property
    def median(self) -> float:
        return self.percentile(50)

    def probability_below(self, value: float) -> float:
        return float((self.terminal < value).mean())

    @property
    def probability_of_loss(self) -> float:
        """Chance of ending with less than was put in, contributions included."""
        return self.probability_below(self.invested)

    def summary(self) -> pd.DataFrame:
        rows = [{"Outcome": f"{p}th percentile", "Value": self.percentile(p),
                 "Total return": self.percentile(p) / self.invested - 1} for p in PERCENTILES]
        return pd.DataFrame(rows)


def _block_indices(n_history: int, horizon: int, paths: int, block: int, rng) -> np.ndarray:
    """Row indices for a moving-block bootstrap.

    Whole blocks of consecutive days are drawn, so streaks and volatility clustering
    survive the resampling. Drawing single days independently would quietly remove
    them and make every outcome look tamer than reality.
    """
    block = max(1, min(block, n_history))
    n_blocks = int(np.ceil(horizon / block))
    starts = rng.integers(0, max(1, n_history - block + 1), size=(paths, n_blocks))
    offsets = np.arange(block)
    idx = (starts[:, :, None] + offsets[None, None, :]).reshape(paths, -1)
    return idx[:, :horizon] % n_history


def simulate_portfolio(returns: pd.Series, start_value: float, *, horizon_years: float = 5.0,
                       paths: int = 2000, block_days: int = 20, drift: str = "historical",
                       annual_drift: float | None = None, monthly_contribution: float = 0.0,
                       seed: int | None = 42) -> Simulation | None:
    """Resample daily returns into a distribution of future values.

    `drift` separates the two things a simulation mixes up. The *shape* of risk always
    comes from history. The *expected return* is a choice: keep history's average
    ("historical"), assume none ("zero"), or state your own ("fixed"). Historical
    averages over a few years are a poor estimate of future returns, so the caller is
    made to choose rather than having one silently assumed.
    """
    r = pd.Series(returns).astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 60 or start_value <= 0 or paths < 1:
        return None
    horizon = max(1, int(round(horizon_years * TRADING_DAYS)))
    rng = np.random.default_rng(seed)
    sample = r.to_numpy()

    if drift == "historical":
        daily_drift = float(sample.mean())
    elif drift == "zero":
        daily_drift = 0.0
    else:
        if annual_drift is None:
            return None
        daily_drift = (1 + annual_drift) ** (1 / TRADING_DAYS) - 1
    centred = sample - sample.mean()                 # keep the shape, replace the average

    idx = _block_indices(len(centred), horizon, paths, block_days, rng)
    draws = centred[idx] + daily_drift

    monthly_step = TRADING_DAYS // 12
    values = np.full(paths, float(start_value))
    contributed = 0.0
    keep = np.linspace(0, horizon - 1, min(horizon, 260)).astype(int)
    tracked = np.empty((paths, len(keep)))
    keep_pos = {d: i for i, d in enumerate(keep)}
    for day in range(horizon):
        values = values * (1.0 + draws[:, day])
        if monthly_contribution and day and day % monthly_step == 0:
            values += monthly_contribution
            contributed += monthly_contribution
        if day in keep_pos:
            tracked[:, keep_pos[day]] = values

    pct = np.percentile(tracked, PERCENTILES, axis=0)
    paths_df = pd.DataFrame(pct.T, columns=[f"p{p}" for p in PERCENTILES],
                            index=(keep / TRADING_DAYS))
    paths_df.index.name = "years"

    a = Assumptions(horizon_years=horizon_years, paths=paths, block_days=block_days, drift=drift,
                    annual_drift=annual_drift, monthly_contribution=monthly_contribution,
                    history_days=len(r), seed=seed)
    sim = Simulation(assumptions=a, start_value=float(start_value), terminal=values,
                     percentile_paths=paths_df, contributed=contributed)
    if len(r) < TRADING_DAYS:
        sim.notes.append(f"Only {len(r)} days of history were available to resample, so the "
                         "range of outcomes is probably too narrow.")
    if drift == "historical":
        sim.notes.append("Expected return comes from this portfolio's own short history, which is "
                         "a weak basis for the future. Compare against the zero-return case.")
    return sim


# ---------------------------------------------------------------- what-if allocations
def portfolio_returns(holding_returns: pd.DataFrame, weights: pd.Series) -> pd.Series:
    """Daily returns of a portfolio held at fixed weights, rebalanced each day."""
    cols = [c for c in holding_returns.columns if c in weights.index]
    if not cols:
        return pd.Series(dtype=float)
    w = weights[cols].astype(float)
    if w.sum() <= 0:
        return pd.Series(dtype=float)
    w = w / w.sum()
    return (holding_returns[cols].fillna(0.0) * w).sum(axis=1)


def equal_weights(assets: list[str]) -> pd.Series:
    return pd.Series(1.0 / len(assets), index=assets) if assets else pd.Series(dtype=float)


def minimum_variance_weights(cov: pd.DataFrame, iterations: int = 5000, tol: float = 1e-10) -> pd.Series:
    """Long-only minimum-variance weights by projected gradient descent.

    Minimum variance needs no return forecast, which is why it is offered here: a
    mean-variance "optimal" portfolio built on estimated returns swings wildly on tiny
    input changes and would be misleading to present as an improvement.
    """
    if cov is None or cov.empty or len(cov) < 2:
        return pd.Series(dtype=float)
    S = cov.to_numpy(dtype=float)
    n = len(S)
    w = np.full(n, 1.0 / n)
    step = 1.0 / (2.0 * max(np.abs(np.linalg.eigvalsh(S)).max(), 1e-12))
    for _ in range(iterations):
        w_new = _project_simplex(w - step * (S @ w))
        if np.abs(w_new - w).max() < tol:
            w = w_new
            break
        w = w_new
    return pd.Series(w, index=cov.index)


def risk_parity_weights(cov: pd.DataFrame, iterations: int = 20000, tol: float = 1e-12) -> pd.Series:
    """Weights where every holding contributes the same share of portfolio risk.

    Solved by the standard fixed-point iteration: repeatedly set each weight to
    target_risk / marginal_risk and renormalise.
    """
    if cov is None or cov.empty or len(cov) < 2:
        return pd.Series(dtype=float)
    S = cov.to_numpy(dtype=float)
    n = len(S)
    w = np.full(n, 1.0 / n)
    target = 1.0 / n
    for _ in range(iterations):
        marginal = S @ w
        vol = float(np.sqrt(w @ S @ w))
        if vol <= 0 or not np.all(np.isfinite(marginal)):
            break
        w_new = target * vol / np.maximum(marginal, 1e-16)
        w_new = np.maximum(w_new, 0.0)
        total = w_new.sum()
        if total <= 0:
            break
        w_new /= total
        if np.abs(w_new - w).max() < tol:
            w = w_new
            break
        w = w_new
    return pd.Series(w, index=cov.index)


def _project_simplex(v: np.ndarray) -> np.ndarray:
    """Nearest point on {w : w >= 0, sum(w) = 1}."""
    u = np.sort(v)[::-1]
    css = np.cumsum(u) - 1.0
    rho = np.nonzero(u - css / np.arange(1, len(v) + 1) > 0)[0][-1]
    return np.maximum(v - css[rho] / (rho + 1.0), 0.0)


# ---------------------------------------------------------------- stress tests
@dataclass
class Stress:
    label: str
    portfolio_change: float
    per_holding: pd.Series
    basis: str


def uniform_shock(weights: pd.Series, betas: pd.Series, market_shock: float,
                  label: str | None = None) -> Stress:
    """A market-wide fall, passed through each holding's beta.

    Idiosyncratic moves are assumed to be zero, so this shows the market component
    only. A real crash would also bring company-specific news.
    """
    b = betas.reindex(weights.index).fillna(1.0).astype(float)
    per = b * market_shock
    return Stress(label=label or f"Market {market_shock:+.0%}",
                  portfolio_change=float((weights * per).sum()),
                  per_holding=(weights * per).sort_values(),
                  basis="each holding moved by its beta times the shock; company-specific moves ignored")


def group_shock(weights: pd.Series, groups: pd.Series, group: str, shock: float) -> Stress:
    """A fall confined to one sector, country or currency bucket."""
    hit = groups.reindex(weights.index).eq(group)
    per = (weights * shock).where(hit, 0.0)
    return Stress(label=f"{group} {shock:+.0%}", portfolio_change=float(per.sum()),
                  per_holding=per[hit].sort_values(),
                  basis=f"only holdings classified as {group} were moved")


def worst_observed(returns: pd.Series, windows: dict[str, int] | None = None) -> pd.DataFrame:
    """The worst stretches this portfolio has actually lived through.

    More grounded than an invented scenario, but limited by a short history: the worst
    thing that has happened is rarely the worst thing that can.
    """
    r = pd.Series(returns).astype(float).dropna()
    if r.empty:
        return pd.DataFrame()
    windows = windows or {"1 day": 1, "1 week": 5, "1 month": 21, "3 months": 63}
    rows = []
    for label, days in windows.items():
        if len(r) <= days:
            continue
        rolled = (1 + r).rolling(days).apply(np.prod, raw=True) - 1
        if rolled.dropna().empty:
            continue
        end = rolled.idxmin()
        rows.append({"Window": label, "Worst": float(rolled.min()),
                     "Ended": end.date() if hasattr(end, "date") else end})
    return pd.DataFrame(rows)
