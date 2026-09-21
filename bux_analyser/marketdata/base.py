"""Provider abstraction. The application only talks to `MarketDataRouter`.

Every value that leaves a provider carries `Provenance` so the UI can always answer
"where did this number come from and how old is it".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class Provenance:
    provider: str            # e.g. "yahoo", "ecb"
    source: str              # human-readable, e.g. "Yahoo Finance (yfinance)"
    retrieved_at: datetime   # when we fetched it
    data_date: date | None   # the date the value refers to
    kind: str = "reported"   # "reported" | "calculated" | "cached"
    note: str = ""

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SecurityIds:
    isin: str
    ticker: str | None = None    # provider-specific symbol, e.g. "ASML.AS"
    currency: str | None = None
    name: str | None = None
    exchange: str | None = None


@dataclass
class Quote:
    price: float
    currency: str
    provenance: Provenance
    previous_close: float | None = None


@dataclass
class PriceSeries:
    """Local-currency EOD closes (adjusted only for splits, not dividends)."""
    series: pd.Series          # index Timestamp, float close
    currency: str
    provenance: Provenance


@dataclass
class SecurityMeta:
    """Descriptive data about a security. Every field may be None: no free source
    covers everything, and the dashboard shows gaps rather than guesses."""
    asset_type: str | None = None      # stock / etf / crypto / unknown
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    market_cap: float | None = None
    long_name: str | None = None
    provenance: Provenance | None = None


@dataclass
class FxSeries:
    series: pd.Series          # base per 1 unit of `currency`
    currency: str
    base: str
    provenance: Provenance


class ProviderError(RuntimeError):
    pass


@runtime_checkable
class PriceProvider(Protocol):
    name: str
    cost: str  # "free" | "free-key" | "paid"

    def resolve(self, isin: str, hint_name: str | None = None, hint_currency: str | None = None) -> SecurityIds | None: ...
    def eod_history(self, ids: SecurityIds, start: date, end: date) -> PriceSeries | None: ...
    def quote(self, ids: SecurityIds) -> Quote | None: ...
    def metadata(self, ids: SecurityIds) -> "SecurityMeta | None": ...


@runtime_checkable
class FxProvider(Protocol):
    name: str
    cost: str

    def fx_history(self, currency: str, base: str, start: date, end: date) -> FxSeries | None: ...


@dataclass
class ProviderHealth:
    name: str
    calls: int = 0
    failures: int = 0
    last_error: str = ""
    last_ok: datetime | None = None
    errors: list[str] = field(default_factory=list)

    def ok(self) -> None:
        self.calls += 1
        self.last_ok = Provenance.now()

    def fail(self, err: Exception | str) -> None:
        self.calls += 1
        self.failures += 1
        self.last_error = str(err)[:300]
        self.errors.append(f"{Provenance.now():%Y-%m-%d %H:%M} {self.last_error}")
        self.errors = self.errors[-20:]
