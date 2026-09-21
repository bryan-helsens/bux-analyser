"""Portfolio-level analytics assembled from holdings, price history and benchmarks.

This module knows nothing about providers or the database: it takes series and
weights and returns an `Analytics` bundle. Anything it cannot compute honestly is
left as None or an empty frame, with the reason recorded in `notes`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .returns import (Decomposition, aggregate_decomposition, average_applied_fx, contribution_to_return,
                      daily_returns, decompose, monthly_table)
from .risk import Concentration, RiskStats, concentration, correlation_matrix, risk_contribution, summarise

ETF_BUCKET = "ETF (not looked through)"
UNKNOWN = "Unknown"


@dataclass
class Exposure:
    """A breakdown of the portfolio by one attribute."""
    dimension: str
    weights: pd.Series               # sums to 1 including the unknown slice
    unknown_weight: float = 0.0
    note: str = ""

    @property
    def known_weight(self) -> float:
        return 1.0 - self.unknown_weight


@dataclass
class BenchmarkComparison:
    label: str
    index: pd.Series                 # rebased to 1.0 over the shared window
    total_return: float | None
    portfolio_total_return: float | None
    stats: RiskStats | None = None
    total_return_basis: bool = True   # False for a price-only index
    note: str = ""

    @property
    def excess(self) -> float | None:
        if self.total_return is None or self.portfolio_total_return is None:
            return None
        return self.portfolio_total_return - self.total_return


@dataclass
class Analytics:
    holding_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    eur_prices: pd.DataFrame = field(default_factory=pd.DataFrame)
    portfolio_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    risk: RiskStats | None = None
    benchmarks: list[BenchmarkComparison] = field(default_factory=list)
    correlation: pd.DataFrame = field(default_factory=pd.DataFrame)
    risk_contribution: pd.DataFrame = field(default_factory=pd.DataFrame)
    concentration: Concentration | None = None
    exposures: dict[str, Exposure] = field(default_factory=dict)
    decomposition: Decomposition | None = None
    monthly: pd.DataFrame = field(default_factory=pd.DataFrame)
    contributions: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    notes: list[str] = field(default_factory=list)


def eur_price_frame(prices: dict[str, pd.Series], fx: dict[str, pd.Series],
                    currency_of: dict[str, str], index: pd.DatetimeIndex,
                    base: str = "EUR") -> pd.DataFrame:
    """Each holding's price converted to the base currency, on one shared calendar.

    Converting before differencing means the daily returns already contain the currency
    effect, which is what a EUR investor actually experiences.
    """
    cols: dict[str, pd.Series] = {}
    for isin, p in prices.items():
        if p is None or p.empty:
            continue
        ccy = currency_of.get(isin, base)
        series = p.sort_index().reindex(index, method="ffill")
        if ccy != base:
            rate = fx.get(ccy)
            if rate is None or rate.empty:
                continue                          # no rate: leave the holding out, never guess
            series = series * rate.sort_index().reindex(index, method="ffill")
        cols[isin] = series
    return pd.DataFrame(cols, index=index) if cols else pd.DataFrame(index=index)


def _bucket(values: dict[str, str | None], weights: pd.Series, dimension: str, note: str = "") -> Exposure:
    labelled = {isin: (values.get(isin) or UNKNOWN) for isin in weights.index}
    grouped = weights.groupby(pd.Series(labelled)).sum().sort_values(ascending=False)
    total = float(grouped.sum())
    if total > 0:
        grouped = grouped / total
    unknown = float(grouped.get(UNKNOWN, 0.0))
    return Exposure(dimension=dimension, weights=grouped, unknown_weight=unknown, note=note)


def build_exposures(weights: pd.Series, meta: dict[str, dict]) -> dict[str, Exposure]:
    """Break the portfolio down by currency, asset type, sector and country.

    ETFs are shown as one bucket rather than spread across sectors: without the fund's
    constituent list, any sector split would be invented. Currency is the listing
    currency, which for an ETF is not the currency of what it holds.
    """
    if weights is None or weights.empty:
        return {}
    w = weights[weights > 0]
    if w.empty:
        return {}
    g = lambda key: {isin: (meta.get(isin) or {}).get(key) for isin in w.index}
    is_etf = {isin: ((meta.get(isin) or {}).get("asset_type") == "etf") for isin in w.index}
    sectors = {isin: (ETF_BUCKET if is_etf[isin] else (meta.get(isin) or {}).get("sector")) for isin in w.index}
    countries = {isin: (ETF_BUCKET if is_etf[isin] else (meta.get(isin) or {}).get("country")) for isin in w.index}
    return {
        "currency": _bucket(g("currency"), w, "Currency",
                            "Listing currency. For an ETF this is not the currency of its holdings."),
        "asset_type": _bucket(g("asset_type"), w, "Asset type"),
        "sector": _bucket(sectors, w, "Sector",
                          "ETFs are shown as one bucket: their constituents are not yet loaded."),
        "country": _bucket(countries, w, "Country",
                           "Country of the company's domicile, not of its revenue."),
    }


def build(holdings, prices, fx, currency_of, meta, twr_index, history_index,
          benchmark_series=None, risk_free: float = 0.0, base: str = "EUR") -> Analytics:
    """Assemble every portfolio analytic we can support from the data available."""
    a = Analytics()
    valued = [h for h in holdings if h.value_base is not None and h.value_base > 0]
    weights = pd.Series({h.isin: float(h.value_base) for h in valued}, dtype=float)
    if not weights.empty:
        weights = weights / weights.sum()

    a.exposures = build_exposures(weights, meta)
    a.concentration = concentration(weights)

    # --- stock versus currency ------------------------------------------------------
    parts = []
    for h in holdings:
        avg_fx = average_applied_fx(h.cost_base, h.cost_local) if h.cost_local else None
        d = decompose(h.price, float(h.avg_cost_local), h.fx, avg_fx,
                      h.price_currency or base, base) if avg_fx else None
        h.decomposition = d
        if d is not None:
            parts.append((d, float(h.cost_base)))
    a.decomposition = aggregate_decomposition(parts)

    # --- return and risk ------------------------------------------------------------
    if twr_index is not None and len(twr_index) > 1:
        a.portfolio_returns = daily_returns(twr_index.dropna())
        a.monthly = monthly_table(twr_index.dropna())

    if history_index is not None and len(history_index):
        eur = eur_price_frame(prices, fx, currency_of, history_index, base)
        a.eur_prices = eur
        held = [c for c in eur.columns if c in weights.index]
        if held:
            a.holding_returns = eur[held].pct_change().replace([np.inf, -np.inf], np.nan)
            window = a.holding_returns.dropna(how="all")
            if len(window) > 1:
                total_by_holding = (1 + a.holding_returns.fillna(0)).prod() - 1
                a.contributions = contribution_to_return(weights, total_by_holding)
        missing = [h.isin for h in valued if h.isin not in eur.columns]
        if missing:
            a.notes.append(f"{len(missing)} holding(s) left out of the risk figures for lack of price history.")

    bench_returns, bench_label = None, None
    a.benchmarks = []
    for label, series, is_total_return, note in (benchmark_series or []):
        if series is None or series.empty or len(series) < 2:
            continue
        s = series.astype(float).dropna()
        if twr_index is not None and len(twr_index.dropna()) > 1:
            pt = twr_index.dropna()
            common = s.index.intersection(pt.index)
            if len(common) < 2:
                continue
            s, pt = s.loc[common], pt.loc[common]
            port_total = float(pt.iloc[-1] / pt.iloc[0] - 1)
        else:
            port_total = None
        rebased = s / s.iloc[0]
        cmp = BenchmarkComparison(label=label, index=rebased, total_return=float(rebased.iloc[-1] - 1),
                                  portfolio_total_return=port_total, total_return_basis=is_total_return,
                                  note=note)
        a.benchmarks.append(cmp)
        if bench_returns is None:
            bench_returns, bench_label = daily_returns(s), label

    a.risk = summarise(a.portfolio_returns, level=twr_index, benchmark_returns=bench_returns,
                       benchmark_name=bench_label, risk_free=risk_free)
    if not a.holding_returns.empty:
        a.correlation = correlation_matrix(a.holding_returns)
        a.risk_contribution = risk_contribution(weights, a.holding_returns)
        if not a.risk_contribution.empty:
            names = {h.isin: (h.name or h.isin) for h in holdings}
            a.risk_contribution.index = [names.get(i, i) for i in a.risk_contribution.index]
            a.correlation.index = [names.get(i, i) for i in a.correlation.index]
            a.correlation.columns = [names.get(i, i) for i in a.correlation.columns]
    if a.risk is not None and not a.risk.reliable:
        a.notes.append(f"Risk figures use only {a.risk.n_obs} days of history; treat them as indicative.")
    return a
