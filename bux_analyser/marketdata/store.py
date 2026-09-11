"""Cache-first market-data access: the app calls `MarketDataStore`, which serves from
SQLite and refreshes incrementally through the router. A provider outage means stale
data plus a visible warning, never an exception in the dashboard."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import BASE_CURRENCY
from ..db import FxRate, PriceEod, QuoteCache, Security
from .base import FxProvider, PriceProvider, Provenance, SecurityIds

log = logging.getLogger(__name__)


class MarketDataRouter:
    """Ordered providers per capability; first success wins."""

    def __init__(self, price_providers: list[PriceProvider], fx_providers: list[FxProvider]):
        self.price_providers = price_providers
        self.fx_providers = fx_providers

    def resolve(self, isin, hint_name=None, hint_currency=None):
        for p in self.price_providers:
            ids = p.resolve(isin, hint_name, hint_currency)
            if ids:
                return p.name, ids
        return None, None

    def eod_history(self, ids, start, end):
        for p in self.price_providers:
            ps = p.eod_history(ids, start, end)
            if ps is not None:
                return ps
        return None

    def quote(self, ids):
        for p in self.price_providers:
            q = p.quote(ids)
            if q is not None:
                return q
        return None

    def fx_history(self, currency, base, start, end):
        for p in self.fx_providers:
            s = p.fx_history(currency, base, start, end)
            if s is not None:
                return s
        return None

    def health(self):
        return [getattr(p, "health", None) for p in self.price_providers + self.fx_providers if getattr(p, "health", None)]


class MarketDataStore:
    def __init__(self, session: Session, router: MarketDataRouter, base: str = BASE_CURRENCY):
        self.s = session
        self.router = router
        self.base = base
        self.warnings: list[str] = []

    # ---- securities -------------------------------------------------------------
    def ensure_resolved(self, isin: str, name: str | None, currency: str | None, force: bool = False) -> Security:
        sec = self.s.get(Security, isin)
        if sec is None:
            sec = Security(isin=isin, name=name, currency=currency)
            self.s.add(sec)
        if sec.ticker and (sec.ticker_manual or not force):
            return sec
        prov, ids = self.router.resolve(isin, name, currency)
        if ids:
            sec.ticker, sec.ticker_provider = ids.ticker, prov
            sec.exchange = ids.exchange
            sec.resolved_at = datetime.now(timezone.utc)
            if not sec.name and ids.name:
                sec.name = ids.name
            if not sec.currency and ids.currency:
                sec.currency = ids.currency
        else:
            self.warnings.append(f"Could not resolve a market symbol for {isin} ({name}); set it manually.")
        self.s.commit()
        return sec

    def set_manual_ticker(self, isin: str, ticker: str) -> None:
        sec = self.s.get(Security, isin)
        sec.ticker, sec.ticker_manual, sec.ticker_provider = ticker, True, "manual"
        self.s.query(PriceEod).filter_by(isin=isin).delete()  # cached prices belonged to the old symbol
        self.s.query(QuoteCache).filter_by(isin=isin).delete()
        self.s.commit()

    # ---- prices -----------------------------------------------------------------
    def price_history(self, isin: str, start: date, end: date | None = None, refresh: bool = True) -> tuple[pd.Series, Provenance | None]:
        end = end or date.today()
        sec = self.s.get(Security, isin)
        if refresh and sec and sec.ticker:
            last = self.s.execute(select(PriceEod.date).where(PriceEod.isin == isin).order_by(PriceEod.date.desc()).limit(1)).scalar()
            fetch_from = start if last is None else max(start, last - timedelta(days=7))  # small overlap: late corrections
            if last is None or last < end:
                ps = self.router.eod_history(SecurityIds(isin, sec.ticker, sec.currency), fetch_from, end)
                if ps is None:
                    self.warnings.append(f"Price refresh failed for {isin} ({sec.ticker}); showing cached data.")
                else:
                    self._upsert_prices(isin, ps.series, ps.currency, ps.provenance)
                    if sec.currency and ps.currency and sec.currency != ps.currency:
                        self.warnings.append(f"{isin}: broker currency {sec.currency} but {sec.ticker} quotes in {ps.currency}; check the symbol.")
        rows = self.s.execute(select(PriceEod).where(PriceEod.isin == isin, PriceEod.date >= start, PriceEod.date <= end).order_by(PriceEod.date)).scalars().all()
        if not rows:
            return pd.Series(dtype=float), None
        s = pd.Series({pd.Timestamp(r.date): float(r.close) for r in rows})
        r = rows[-1]
        return s, Provenance(r.provider, r.source, r.retrieved_at, r.date, "cached")

    def _upsert_prices(self, isin, series: pd.Series, currency: str, prov: Provenance) -> None:
        existing = {r.date: r for r in self.s.execute(select(PriceEod).where(PriceEod.isin == isin, PriceEod.date >= series.index.min().date())).scalars()}
        for ts, close in series.items():
            d = ts.date()
            row = existing.get(d)
            if row is None:
                self.s.add(PriceEod(isin=isin, date=d, close=Decimal(str(round(close, 8))), currency=currency,
                                    provider=prov.provider, source=prov.source, retrieved_at=prov.retrieved_at))
            else:
                row.close, row.retrieved_at = Decimal(str(round(close, 8))), prov.retrieved_at
        self.s.commit()

    def quote(self, isin: str, max_age: timedelta = timedelta(minutes=20), refresh: bool = True) -> QuoteCache | None:
        q = self.s.get(QuoteCache, isin)
        fresh = q is not None and (datetime.now(timezone.utc) - q.retrieved_at.replace(tzinfo=timezone.utc)) < max_age
        if fresh or not refresh:
            return q
        sec = self.s.get(Security, isin)
        if not sec or not sec.ticker:
            return q
        res = self.router.quote(SecurityIds(isin, sec.ticker, sec.currency))
        if res is None:
            self.warnings.append(f"Quote refresh failed for {isin}; using last cached quote.")
            return q
        if q is None:
            q = QuoteCache(isin=isin)
            self.s.add(q)
        q.price, q.previous_close, q.currency = Decimal(str(res.price)), (Decimal(str(res.previous_close)) if res.previous_close else None), res.currency
        q.provider, q.source, q.retrieved_at, q.note = res.provenance.provider, res.provenance.source, res.provenance.retrieved_at, res.provenance.note
        self.s.commit()
        return q

    # ---- fx ---------------------------------------------------------------------
    def fx_history(self, currency: str, start: date, end: date | None = None, refresh: bool = True) -> tuple[pd.Series, Provenance | None]:
        end = end or date.today()
        if currency == self.base:
            return pd.Series(1.0, index=pd.date_range(start, end)), Provenance("identity", "identity", Provenance.now(), end, "calculated")
        if refresh:
            last = self.s.execute(select(FxRate.date).where(FxRate.currency == currency, FxRate.base == self.base).order_by(FxRate.date.desc()).limit(1)).scalar()
            if last is None or last < end:
                fs = self.router.fx_history(currency, self.base, start if last is None else last, end)
                if fs is None:
                    self.warnings.append(f"FX refresh failed for {currency}; using cached rates.")
                else:
                    existing = {r.date for r in self.s.execute(select(FxRate).where(FxRate.currency == currency, FxRate.base == self.base, FxRate.date >= fs.series.index.min().date())).scalars()}
                    for ts, rate in fs.series.items():
                        if ts.date() not in existing:
                            self.s.add(FxRate(currency=currency, base=self.base, date=ts.date(), rate=Decimal(str(rate)),
                                              provider=fs.provenance.provider, source=fs.provenance.source, retrieved_at=fs.provenance.retrieved_at))
                    self.s.commit()
        rows = self.s.execute(select(FxRate).where(FxRate.currency == currency, FxRate.base == self.base, FxRate.date >= start, FxRate.date <= end).order_by(FxRate.date)).scalars().all()
        if not rows:
            return pd.Series(dtype=float), None
        r = rows[-1]
        return pd.Series({pd.Timestamp(x.date): float(x.rate) for x in rows}), Provenance(r.provider, r.source, r.retrieved_at, r.date, "cached")
