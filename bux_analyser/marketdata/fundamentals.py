"""Company fundamentals: the shared model every source is normalised into.

Different sources disagree about names, units and even about what a period is, so
adapters translate into this one shape. Two fields matter more than they look:

- `as_reported_at` is the date the figure was *published*, not the date the period
  ended. Using period-end dates in a backtest silently gives the strategy numbers
  nobody had yet, which is the most common way a fundamental backtest flatters itself.
- `quality` separates an audited filing from a scraped best guess, so the dashboard can
  show where a number came from instead of implying they are all equal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from .base import Provenance, SecurityIds

REPORTED = "reported"        # from a filing, as published
CALCULATED = "calculated"    # derived by us from reported figures
UNVERIFIED = "unverified"    # best-effort scrape; may be wrong or stale

FY, Q, TTM = "FY", "Q", "TTM"

# The metrics every adapter tries to fill. Anything absent stays absent.
LINE_ITEMS = (
    "revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
    "operating_cash_flow", "capex", "cash", "total_debt", "total_equity",
    "total_assets", "shares_diluted", "dividends_paid",
)


@dataclass
class FinancialPeriod:
    period_end: date
    period_type: str                       # FY | Q | TTM
    values: dict[str, float] = field(default_factory=dict)
    as_reported_at: date | None = None
    currency: str | None = None
    source: str = ""
    quality: str = REPORTED

    def get(self, key: str) -> float | None:
        v = self.values.get(key)
        return None if v is None else float(v)

    @property
    def free_cash_flow(self) -> float | None:
        """Operating cash flow less capital expenditure.

        Sources report capex with inconsistent signs, so its magnitude is used.
        """
        ocf, capex = self.get("operating_cash_flow"), self.get("capex")
        if ocf is None:
            return None
        return ocf - abs(capex) if capex is not None else None

    @property
    def net_debt(self) -> float | None:
        debt, cash = self.get("total_debt"), self.get("cash")
        if debt is None:
            return None
        return debt - (cash or 0.0)


@dataclass
class Fundamentals:
    isin: str
    ticker: str | None = None
    currency: str | None = None
    periods: list[FinancialPeriod] = field(default_factory=list)
    provenance: Provenance | None = None
    notes: list[str] = field(default_factory=list)

    def sorted_periods(self, period_type: str | None = None) -> list[FinancialPeriod]:
        rows = [p for p in self.periods if period_type is None or p.period_type == period_type]
        return sorted(rows, key=lambda p: p.period_end)

    def latest(self, period_type: str = FY, as_of: date | None = None) -> FinancialPeriod | None:
        """The most recent period, optionally restricted to what was published by `as_of`.

        Passing `as_of` is what makes a point-in-time view possible: it hides figures
        that had not been filed yet on that date.
        """
        rows = self.sorted_periods(period_type)
        if as_of is not None:
            rows = [p for p in rows if p.as_reported_at is not None and p.as_reported_at <= as_of]
        return rows[-1] if rows else None

    def annual_series(self, key: str, as_of: date | None = None) -> list[tuple[date, float]]:
        out = []
        for p in self.sorted_periods(FY):
            if as_of is not None and (p.as_reported_at is None or p.as_reported_at > as_of):
                continue
            v = p.get(key)
            if v is not None:
                out.append((p.period_end, v))
        return out

    def trailing_twelve_months(self, as_of: date | None = None) -> FinancialPeriod | None:
        """Sum the last four quarters for flow items; take the latest for stock items.

        Falls back to the latest full year when quarterly data is not available, which is
        the normal case for European filers.
        """
        quarters = self.sorted_periods(Q)
        if as_of is not None:
            quarters = [q for q in quarters if q.as_reported_at is not None and q.as_reported_at <= as_of]
        if len(quarters) < 4:
            return self.latest(FY, as_of)
        recent = quarters[-4:]
        # Balance-sheet items are carried forward from the most recent period that
        # reported them: a quarterly filing often omits figures the annual report gave,
        # and dropping them would silently disable every ratio that needs a denominator.
        earlier = [p for p in self.sorted_periods()
                   if p.period_end <= recent[-1].period_end
                   and (as_of is None or (p.as_reported_at and p.as_reported_at <= as_of))]
        values: dict[str, float] = {}
        for key in LINE_ITEMS:
            if key in FLOW_ITEMS:
                parts = [q.get(key) for q in recent]
                if all(v is not None for v in parts):
                    values[key] = float(sum(parts))
            else:
                for period in reversed(earlier):
                    v = period.get(key)
                    if v is not None:
                        values[key] = v
                        break
        if not values:
            return self.latest(FY, as_of)
        return FinancialPeriod(period_end=recent[-1].period_end, period_type=TTM, values=values,
                               as_reported_at=recent[-1].as_reported_at, currency=self.currency,
                               source=recent[-1].source, quality=CALCULATED)

    @property
    def is_empty(self) -> bool:
        return not self.periods

    @property
    def stale_days(self) -> int | None:
        rows = [p.as_reported_at for p in self.periods if p.as_reported_at]
        return (date.today() - max(rows)).days if rows else None


FLOW_ITEMS = {"revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
              "operating_cash_flow", "capex", "dividends_paid"}
STOCK_ITEMS = {"cash", "total_debt", "total_equity", "total_assets", "shares_diluted"}


@runtime_checkable
class FundamentalsProvider(Protocol):
    name: str
    cost: str

    def fundamentals(self, ids: SecurityIds) -> Fundamentals | None: ...


def to_json(f: "Fundamentals") -> dict:
    return {
        "isin": f.isin, "ticker": f.ticker, "currency": f.currency, "notes": f.notes,
        "periods": [{"period_end": p.period_end.isoformat(), "period_type": p.period_type,
                     "values": p.values,
                     "as_reported_at": p.as_reported_at.isoformat() if p.as_reported_at else None,
                     "currency": p.currency, "source": p.source, "quality": p.quality}
                    for p in f.periods],
    }


def from_json(payload: dict) -> "Fundamentals":
    from datetime import date as _date
    parse = lambda v: _date.fromisoformat(v) if v else None
    return Fundamentals(
        isin=payload.get("isin", ""), ticker=payload.get("ticker"),
        currency=payload.get("currency"), notes=list(payload.get("notes") or []),
        periods=[FinancialPeriod(period_end=parse(p["period_end"]), period_type=p["period_type"],
                                 values={k: float(v) for k, v in (p.get("values") or {}).items()},
                                 as_reported_at=parse(p.get("as_reported_at")),
                                 currency=p.get("currency"), source=p.get("source", ""),
                                 quality=p.get("quality", REPORTED))
                 for p in payload.get("periods", [])])
