"""Valuation, growth and quality metrics derived from financial statements.

Pure arithmetic over a `Fundamentals` bundle. Every ratio returns None rather than a
guess when an input is missing, because a valuation built on a silently defaulted
figure is worse than no valuation at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from ..marketdata.fundamentals import FY, TTM, FinancialPeriod, Fundamentals

TAX_RATE = 0.25          # a flat assumption for return on capital; stated, not hidden


@dataclass
class FundamentalMetrics:
    isin: str
    as_of: date
    currency: str | None = None
    values: dict[str, float] = field(default_factory=dict)
    period_end: date | None = None
    reported_at: date | None = None
    quality: str = "reported"
    notes: list[str] = field(default_factory=list)

    def get(self, key: str) -> float | None:
        v = self.values.get(key)
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)

    @property
    def stale_days(self) -> int | None:
        return (self.as_of - self.reported_at).days if self.reported_at else None


def _ratio(numerator: float | None, denominator: float | None, *, positive_only: bool = False):
    if numerator is None or denominator is None or denominator == 0:
        return None
    if positive_only and denominator <= 0:
        return None          # a negative denominator makes the ratio meaningless, not negative
    return float(numerator) / float(denominator)


def _cagr(series: list[tuple[date, float]], years: int) -> float | None:
    """Compound growth between the earliest and latest points in the window.

    Refuses when the starting value is not positive: growth from a loss is not a
    percentage anybody can interpret.
    """
    if len(series) < 2:
        return None
    window = series[-(years + 1):]
    if len(window) < 2:
        return None
    (start_date, start), (end_date, end) = window[0], window[-1]
    span = (end_date - start_date).days / 365.25
    if start <= 0 or span <= 0:
        return None
    return float((end / start) ** (1 / span) - 1)


def compute(f: Fundamentals, market_cap: float | None, as_of: date,
            price: float | None = None, as_reported_by: date | None = None) -> FundamentalMetrics | None:
    """Derive the metric set.

    `market_cap` and `price` must be expressed in the same currency as the statements,
    which the caller is responsible for converting; mixing a euro price with dollar
    revenue would silently corrupt every valuation ratio.
    """
    if f is None or f.is_empty:
        return None
    latest = f.trailing_twelve_months(as_reported_by) or f.latest(FY, as_reported_by)
    if latest is None:
        return None
    m = FundamentalMetrics(isin=f.isin, as_of=as_of, currency=f.currency,
                           period_end=latest.period_end, reported_at=latest.as_reported_at,
                           quality=latest.quality)

    revenue = latest.get("revenue")
    net_income = latest.get("net_income")
    operating_income = latest.get("operating_income")
    gross_profit = latest.get("gross_profit")
    equity = latest.get("total_equity")
    assets = latest.get("total_assets")
    fcf = latest.free_cash_flow
    net_debt = latest.net_debt
    eps = latest.get("eps_diluted")

    # --- valuation ------------------------------------------------------------
    if market_cap:
        m.values["market_cap"] = market_cap
        m.values["price_to_sales"] = _ratio(market_cap, revenue, positive_only=True)
        m.values["price_to_book"] = _ratio(market_cap, equity, positive_only=True)
        m.values["price_to_earnings"] = _ratio(market_cap, net_income, positive_only=True)
        m.values["fcf_yield"] = _ratio(fcf, market_cap)
        m.values["earnings_yield"] = _ratio(net_income, market_cap)
        if net_debt is not None and operating_income:
            ev = market_cap + net_debt
            m.values["ev_to_ebit"] = _ratio(ev, operating_income, positive_only=True)
    if price is not None and eps:
        m.values.setdefault("price_to_earnings", _ratio(price, eps, positive_only=True))

    # --- growth ---------------------------------------------------------------
    for key, label in (("revenue", "revenue"), ("net_income", "earnings"), ("eps_diluted", "eps")):
        series = f.annual_series(key, as_reported_by)
        m.values[f"{label}_growth_1y"] = _cagr(series, 1)
        m.values[f"{label}_cagr_3y"] = _cagr(series, 3)
    fcf_series = [(p.period_end, p.free_cash_flow) for p in f.sorted_periods(FY)
                  if p.free_cash_flow is not None
                  and (as_reported_by is None or (p.as_reported_at and p.as_reported_at <= as_reported_by))]
    m.values["fcf_cagr_3y"] = _cagr(fcf_series, 3)

    # --- quality --------------------------------------------------------------
    m.values["gross_margin"] = _ratio(gross_profit, revenue, positive_only=True)
    m.values["operating_margin"] = _ratio(operating_income, revenue, positive_only=True)
    m.values["net_margin"] = _ratio(net_income, revenue, positive_only=True)
    m.values["return_on_equity"] = _ratio(net_income, equity, positive_only=True)
    m.values["return_on_assets"] = _ratio(net_income, assets, positive_only=True)
    if operating_income is not None and equity is not None and net_debt is not None:
        capital = equity + max(net_debt, 0.0)
        m.values["return_on_capital"] = _ratio(operating_income * (1 - TAX_RATE), capital,
                                               positive_only=True)
    m.values["fcf_conversion"] = _ratio(fcf, net_income, positive_only=True)
    m.values["debt_to_equity"] = _ratio(latest.get("total_debt"), equity, positive_only=True)
    m.values["net_debt_to_ebit"] = _ratio(net_debt, operating_income, positive_only=True)

    m.values = {k: v for k, v in m.values.items() if v is not None}
    if latest.period_type == FY and latest.as_reported_at:
        age = (as_of - latest.as_reported_at).days
        if age > 400:
            m.notes.append(f"Latest figures were filed {age} days ago and may no longer describe "
                           "the business.")
    if latest.quality == "unverified":
        m.notes.append("Figures come from a scraped source with no filing date; treat them as "
                       "indicative.")
    return m


def valuation_vs_history(f: Fundamentals, current_ratio: float | None, ratio_key: str,
                         price_history, shares: float | None) -> float | None:
    """Where a valuation ratio sits against its own past, 0 to 100.

    Comparing a company with itself sidesteps the need for a peer universe, which no
    free source provides at the breadth this would need.
    """
    if current_ratio is None or price_history is None or len(price_history) < 60 or not shares:
        return None
    earnings = [(p.period_end, p.get("net_income")) for p in f.sorted_periods(FY)
                if p.get("net_income")]
    if len(earnings) < 3:
        return None
    ratios = []
    for period_end, income in earnings:
        window = price_history[price_history.index <= str(period_end)]
        if window.empty or income <= 0:
            continue
        ratios.append(float(window.iloc[-1]) * shares / income)
    if len(ratios) < 3:
        return None
    return float((np.array(ratios) > current_ratio).mean() * 100)
