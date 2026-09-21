"""Return analysis: period tables and the split between stock and currency performance.

Every function is pure and takes plain pandas objects, so the maths can be tested
without a database or a network.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass(frozen=True)
class Decomposition:
    """Exact split of a EUR investor's return into stock and currency components.

    For a position bought at average cost `c` in local currency at an average applied
    rate `f` (base per local unit), and now worth `p` at rate `f1`:

        value / cost = (p / c) * (f1 / f)

    so the total return factors exactly into a local-currency return and an FX return.
    The cross term is reported separately because `local + fx` alone does not add up.
    """
    local: float          # return from the security's price, in its own currency
    fx: float             # return from the exchange rate
    total: float          # (1+local)(1+fx) - 1
    cross: float          # local * fx, the interaction term
    base_currency: str = "EUR"
    local_currency: str = "EUR"

    @property
    def is_single_currency(self) -> bool:
        return self.local_currency == self.base_currency


def decompose(price_now: float, avg_cost_local: float, fx_now: float, avg_fx: float,
              local_currency: str = "EUR", base_currency: str = "EUR") -> Decomposition | None:
    """Split a position's return. Returns None when the inputs cannot support it."""
    if not avg_cost_local or not avg_fx or price_now is None or fx_now is None:
        return None
    r_local = price_now / avg_cost_local - 1.0
    r_fx = fx_now / avg_fx - 1.0
    return Decomposition(local=r_local, fx=r_fx, total=(1 + r_local) * (1 + r_fx) - 1.0,
                         cross=r_local * r_fx, local_currency=local_currency, base_currency=base_currency)


def average_applied_fx(cost_base: Decimal | float, cost_local: Decimal | float) -> float | None:
    """The effective rate at which a position's cost was converted to the base currency."""
    cl = float(cost_local)
    return (float(cost_base) / cl) if cl else None


def aggregate_decomposition(parts: list[tuple[Decomposition, float]]) -> Decomposition | None:
    """Combine per-position decompositions, weighted by cost basis in the base currency.

    Weighting by cost makes the aggregate total equal the portfolio's own cost-to-value
    return, so the parts reconcile with the whole.
    """
    parts = [(d, w) for d, w in parts if d is not None and w]
    if not parts:
        return None
    tw = sum(w for _, w in parts)
    if not tw:
        return None
    g = lambda f: sum(f(d) * w for d, w in parts) / tw
    return Decomposition(local=g(lambda d: d.local), fx=g(lambda d: d.fx),
                         total=g(lambda d: d.total), cross=g(lambda d: d.cross),
                         local_currency="mixed" if len({d.local_currency for d, _ in parts}) > 1 else parts[0][0].local_currency)


def daily_returns(index_series: pd.Series) -> pd.Series:
    """Daily returns from a level series (a TWR index or a price series)."""
    if index_series is None or len(index_series) < 2:
        return pd.Series(dtype=float)
    return index_series.astype(float).pct_change().dropna()


def period_returns(twr_index: pd.Series, freq: str = "ME") -> pd.Series:
    """Compound a TWR index into period returns. freq: 'ME' monthly, 'YE' yearly."""
    if twr_index is None or twr_index.empty:
        return pd.Series(dtype=float)
    lvl = twr_index.astype(float).resample(freq).last()
    prev = lvl.shift(1)
    # The index opens at its own first value, so that is the base for the first period.
    prev.iloc[0] = float(twr_index.iloc[0])
    return (lvl / prev - 1.0).replace([float("inf"), float("-inf")], float("nan")).dropna()


def monthly_table(twr_index: pd.Series) -> pd.DataFrame:
    """Years as rows, months as columns, plus a compounded year total."""
    m = period_returns(twr_index, "ME")
    if m.empty:
        return pd.DataFrame()
    df = pd.DataFrame({"year": m.index.year, "month": m.index.month, "r": m.to_numpy()})
    table = df.pivot(index="year", columns="month", values="r")
    table.columns = [pd.Timestamp(2000, c, 1).strftime("%b") for c in table.columns]
    table["Year"] = df.groupby("year")["r"].apply(lambda s: (1 + s).prod() - 1)
    return table


def cumulative(returns: pd.Series) -> pd.Series:
    """Cumulative growth of 1 unit."""
    return (1 + returns.astype(float)).cumprod() if len(returns) else pd.Series(dtype=float)


def annualise(total_return: float, days: int) -> float | None:
    """Convert a cumulative return over `days` calendar days to an annual rate."""
    if days <= 0 or total_return <= -1:
        return None
    years = days / 365.25
    return (1 + total_return) ** (1 / years) - 1 if years > 0 else None


def contribution_to_return(weights: pd.Series, returns: pd.Series) -> pd.Series:
    """Each holding's share of the portfolio return over a window (weight x return)."""
    common = weights.index.intersection(returns.index)
    return (weights[common] * returns[common]).sort_values(ascending=False)
