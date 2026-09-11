from datetime import date, timedelta

import pandas as pd

from bux_analyser.db import PriceEod, make_engine, session_factory
from bux_analyser.marketdata.base import FxSeries, PriceSeries, Provenance, Quote, SecurityIds
from bux_analyser.marketdata.store import MarketDataRouter, MarketDataStore


class StubPrices:
    name, cost = "stub", "free"

    def __init__(self):
        self.calls = 0
        self.fail = False

    def resolve(self, isin, hint_name=None, hint_currency=None):
        return SecurityIds(isin, "STUB.AS", "EUR", "Stub NV", "AMS")

    def eod_history(self, ids, start, end):
        self.calls += 1
        if self.fail:
            return None
        idx = pd.date_range(start, end, freq="B")
        return PriceSeries(pd.Series(range(1, len(idx) + 1), index=idx, dtype=float), "EUR",
                           Provenance("stub", "Stub source", Provenance.now(), end))

    def quote(self, ids):
        return None if self.fail else Quote(42.0, "EUR", Provenance("stub", "Stub source", Provenance.now(), date.today()), 41.0)


class StubFx:
    name, cost = "stubfx", "free"

    def fx_history(self, currency, base, start, end):
        idx = pd.date_range(start, end, freq="B")
        return FxSeries(pd.Series(0.9, index=idx), currency, base, Provenance("stubfx", "Stub FX", Provenance.now(), end))


def make_store(tmp_path):
    eng = make_engine(tmp_path / "t.db")
    sp = StubPrices()
    store = MarketDataStore(session_factory(eng)(), MarketDataRouter([sp], [StubFx()]))
    return store, sp


def test_resolve_cache_refresh_and_provenance(tmp_path):
    store, sp = make_store(tmp_path)
    sec = store.ensure_resolved("NL0000000001", "Stub", "EUR")
    assert sec.ticker == "STUB.AS" and sec.ticker_provider == "stub"
    start = date.today() - timedelta(days=30)
    s1, prov = store.price_history("NL0000000001", start)
    assert not s1.empty and prov.provider == "stub" and prov.kind == "cached"
    assert sp.calls == 1
    # cached & up to date → no provider call, same data
    s2, _ = store.price_history("NL0000000001", start)
    assert sp.calls == 1 and s2.equals(s1)
    # provider outage → stale data served with a warning, no exception
    sp.fail = True
    store.s.query(PriceEod).filter_by(date=s1.index[-1].date()).delete()
    store.s.commit()
    s3, _ = store.price_history("NL0000000001", start)
    assert len(s3) == len(s1) - 1 and any("Price refresh failed" in w for w in store.warnings)
    assert store.quote("NL0000000001") is None
    sp.fail = False
    q = store.quote("NL0000000001")
    assert float(q.price) == 42.0 and q.provider == "stub"
    q2 = store.quote("NL0000000001")  # fresh → cached object
    assert q2.retrieved_at == q.retrieved_at


def test_manual_ticker_override_clears_cache(tmp_path):
    store, sp = make_store(tmp_path)
    store.ensure_resolved("NL0000000001", "Stub", "EUR")
    store.price_history("NL0000000001", date.today() - timedelta(days=10))
    store.set_manual_ticker("NL0000000001", "OTHER.DE")
    sec = store.ensure_resolved("NL0000000001", "Stub", "EUR", force=True)
    assert sec.ticker == "OTHER.DE" and sec.ticker_manual
    s, prov = store.price_history("NL0000000001", date.today() - timedelta(days=10), refresh=False)
    assert s.empty and prov is None


def test_fx_identity_and_cached(tmp_path):
    store, _ = make_store(tmp_path)
    eur, prov = store.fx_history("EUR", date.today() - timedelta(days=5))
    assert (eur == 1.0).all() and prov.kind == "calculated"
    usd, prov = store.fx_history("USD", date.today() - timedelta(days=5))
    assert (usd == 0.9).all() and prov.provider == "stubfx"
