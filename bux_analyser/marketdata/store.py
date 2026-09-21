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
from ..db import EtfHolding, FundamentalsCache, FxRate, PriceEod, QuoteCache, Security
import json

from .base import FxProvider, PriceProvider, Provenance, SecurityIds, SecurityMeta
from .fundamentals import Fundamentals, from_json, to_json

log = logging.getLogger(__name__)


class MarketDataRouter:
    """Ordered providers per capability; the first usable answer wins.

    A provider that raises is treated exactly like one that returns nothing: the error
    is recorded against its health and the next provider is tried. Providers are
    pluggable, so the router cannot assume they all handle their own failures.
    """

    def __init__(self, price_providers: list[PriceProvider], fx_providers: list[FxProvider],
                 fundamental_providers: list | None = None):
        self.price_providers = price_providers
        self.fx_providers = fx_providers
        # Tried before the price providers' own best-effort statements, so an official
        # filing always beats a scraped one.
        self.fundamental_providers = fundamental_providers or []

    @staticmethod
    def _call(provider, method: str, *args):
        fn = getattr(provider, method, None)
        if fn is None:
            return None
        try:
            return fn(*args)
        except Exception as e:                       # noqa: BLE001 - deliberately broad
            health = getattr(provider, "health", None)
            if health is not None:
                health.fail(e)
            log.warning("provider %s.%s failed: %s", getattr(provider, "name", provider), method, e)
            return None

    def _first(self, providers, method: str, *args):
        for p in providers:
            result = self._call(p, method, *args)
            if result is not None:
                return p, result
        return None, None

    def resolve(self, isin, hint_name=None, hint_currency=None):
        p, ids = self._first(self.price_providers, "resolve", isin, hint_name, hint_currency)
        return (p.name if p else None), ids

    def eod_history(self, ids, start, end):
        return self._first(self.price_providers, "eod_history", ids, start, end)[1]

    def quote(self, ids):
        return self._first(self.price_providers, "quote", ids)[1]

    def metadata(self, ids):
        return self._first(self.price_providers, "metadata", ids)[1]

    def fx_history(self, currency, base, start, end):
        return self._first(self.fx_providers, "fx_history", currency, base, start, end)[1]

    def health(self):
        seen = []
        for p in self.price_providers + self.fx_providers + self.fundamental_providers:
            h = getattr(p, "health", None)
            if h is not None and h not in seen:
                seen.append(h)
        return seen


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

    def ensure_metadata(self, isin: str, max_age_days: int = 30, force: bool = False) -> Security | None:
        """Fetch sector/industry/country once a month. Missing fields stay missing."""
        sec = self.s.get(Security, isin)
        if sec is None or not sec.ticker:
            return sec
        fresh = sec.meta_retrieved_at is not None and (
            datetime.now(timezone.utc) - sec.meta_retrieved_at.replace(tzinfo=timezone.utc)
        ) < timedelta(days=max_age_days)
        if fresh and not force:
            return sec
        m = self.router.metadata(SecurityIds(isin, sec.ticker, sec.currency))
        if m is None:
            self.warnings.append(f"No profile data available for {sec.name or isin}.")
            return sec
        sec.sector = m.sector or sec.sector
        sec.industry = m.industry or sec.industry
        sec.country = m.country or sec.country
        sec.market_cap = Decimal(str(m.market_cap)) if m.market_cap else sec.market_cap
        if m.asset_type and sec.asset_type in (None, "security", "unknown"):
            sec.asset_type = m.asset_type
        sec.meta_provider = m.provenance.provider if m.provenance else None
        sec.meta_retrieved_at = datetime.now(timezone.utc)
        self.s.commit()
        return sec

    def ensure_benchmark(self, benchmark, start: date, end: date | None = None):
        """Benchmarks are ordinary securities under a reserved key, so they reuse the
        same cache, provenance and degradation behaviour as holdings."""
        sec = self.s.get(Security, benchmark.key)
        if sec is None:
            sec = Security(isin=benchmark.key, name=benchmark.label, ticker=benchmark.ticker,
                           ticker_provider="config", ticker_manual=True, asset_type="benchmark",
                           currency="EUR")
            self.s.add(sec)
            self.s.commit()
        return self.price_history(benchmark.key, start, end)

    # ---- fundamentals -----------------------------------------------------------
    def ensure_fundamentals(self, isin: str, max_age_days: int = 7, force: bool = False):
        """Fetch and cache company statements. Quarterly filings arrive at most a few
        times a year, so a weekly refresh is generous."""
        sec = self.s.get(Security, isin)
        if sec is None or not sec.ticker or sec.asset_type in ("etf", "crypto", "benchmark"):
            return None
        for provider in list(self.router.fundamental_providers) + list(self.router.price_providers):
            fn = getattr(provider, "fundamentals", None)
            if fn is None:
                continue
            row = self.s.execute(select(FundamentalsCache).where(
                FundamentalsCache.isin == isin, FundamentalsCache.provider == provider.name)).scalar()
            fresh = row is not None and (datetime.now(timezone.utc)
                                         - row.retrieved_at.replace(tzinfo=timezone.utc)) < timedelta(days=max_age_days)
            if fresh and not force:
                continue
            data = self.router._call(provider, "fundamentals", SecurityIds(isin, sec.ticker, sec.currency))
            if data is None or data.is_empty:
                continue
            payload = json.dumps(to_json(data))
            quality = data.periods[-1].quality if data.periods else "reported"
            latest = max((p.as_reported_at for p in data.periods if p.as_reported_at), default=None)
            if row is None:
                self.s.add(FundamentalsCache(isin=isin, provider=provider.name, payload=payload,
                                             currency=data.currency, quality=quality,
                                             latest_report=latest,
                                             retrieved_at=datetime.now(timezone.utc)))
            else:
                row.payload, row.currency, row.quality = payload, data.currency, quality
                row.latest_report, row.retrieved_at = latest, datetime.now(timezone.utc)
            self.s.commit()
        return self.get_fundamentals(isin)

    def get_fundamentals(self, isin: str) -> Fundamentals | None:
        """Read from cache only. Prefers an authoritative filing over a scraped one."""
        rows = list(self.s.execute(select(FundamentalsCache).where(
            FundamentalsCache.isin == isin)).scalars())
        if not rows:
            return None
        rows.sort(key=lambda r: (r.quality != "reported", -(r.latest_report or date.min).toordinal()))
        return from_json(json.loads(rows[0].payload))

    # ---- ETF constituents ---------------------------------------------------------
    def ensure_etf_holdings(self, isin: str, provider, max_age_days: int = 30, force: bool = False):
        latest = self.s.execute(select(EtfHolding.as_of).where(EtfHolding.etf_isin == isin)
                                .order_by(EtfHolding.as_of.desc()).limit(1)).scalar()
        if latest is not None and not force and (date.today() - latest).days < max_age_days:
            return self.get_etf_holdings(isin)
        file = self.router._call(provider, "holdings", isin)
        if file is None:
            self.warnings.append(f"No holdings file available for {isin}; the fund stays "
                                 "unbroken in exposure charts.")
            return self.get_etf_holdings(isin)
        self.s.query(EtfHolding).filter_by(etf_isin=isin, as_of=file.as_of).delete()
        now = datetime.now(timezone.utc)
        for c in file.constituents:
            self.s.add(EtfHolding(etf_isin=isin, as_of=file.as_of, constituent=c.identifier,
                                  name=c.name, weight=Decimal(str(c.weight)), sector=c.sector,
                                  country=c.country, currency=c.currency,
                                  asset_class=c.asset_class, source=file.source, retrieved_at=now))
        self.s.commit()
        return self.get_etf_holdings(isin)

    def get_etf_holdings(self, isin: str):
        latest = self.s.execute(select(EtfHolding.as_of).where(EtfHolding.etf_isin == isin)
                                .order_by(EtfHolding.as_of.desc()).limit(1)).scalar()
        if latest is None:
            return None
        rows = self.s.execute(select(EtfHolding).where(EtfHolding.etf_isin == isin,
                                                       EtfHolding.as_of == latest)).scalars()
        return pd.DataFrame([{"constituent": r.constituent, "name": r.name,
                              "weight": float(r.weight), "sector": r.sector, "country": r.country,
                              "currency": r.currency, "asset_class": r.asset_class} for r in rows])

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
