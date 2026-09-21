from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from bux_analyser.db import make_engine, session_factory
from bux_analyser.importers.persist import import_bux_file
from bux_analyser.marketdata.base import (FxSeries, PriceSeries, Provenance, Quote, SecurityIds,
                                          SecurityMeta)
from bux_analyser.marketdata.store import MarketDataRouter, MarketDataStore
from bux_analyser.service import build_snapshot, refresh_market_data

FIX = Path(__file__).parent / "fixtures" / "bux_synthetic.csv"


class Prices:
    name, cost = "stub", "free"
    levels = {"US0000000001": (120.0, "USD"), "NL0000000002": (16.0, "EUR")}

    def resolve(self, isin, hint_name=None, hint_currency=None):
        return SecurityIds(isin, isin[:4], self.levels[isin][1]) if isin in self.levels else None

    def eod_history(self, ids, start, end):
        if ids.isin not in self.levels:
            return None                       # unknown symbol, e.g. a benchmark
        lvl, ccy = self.levels[ids.isin]
        idx = pd.date_range(start, end, freq="D")
        return PriceSeries(pd.Series(lvl, index=idx), ccy, Provenance("stub", "Stub", Provenance.now(), end))

    def quote(self, ids):
        if ids.isin not in self.levels:
            return None
        lvl, ccy = self.levels[ids.isin]
        return Quote(lvl, ccy, Provenance("stub", "Stub", Provenance.now(), date.today()), previous_close=lvl / 1.1)

    profiles = {"US0000000001": ("stock", "Technology", "United States"),
                "NL0000000002": ("etf", None, None)}

    def metadata(self, ids):
        if ids.isin not in self.profiles:
            return None
        kind, sector, country = self.profiles[ids.isin]
        return SecurityMeta(asset_type=kind, sector=sector, country=country, market_cap=1e9,
                            provenance=Provenance("stub", "Stub", Provenance.now(), date.today()))


class Fx:
    name, cost = "fx", "free"

    def fx_history(self, currency, base, start, end):
        idx = pd.date_range(start, end, freq="D")
        return FxSeries(pd.Series(0.5, index=idx), currency, base, Provenance("fx", "Stub FX", Provenance.now(), end))


def test_snapshot_values_and_provenance(tmp_path):
    s = session_factory(make_engine(tmp_path / "t.db"))()
    import_bux_file(s, FIX, archive=False)
    store = MarketDataStore(s, MarketDataRouter([Prices()], [Fx()]))
    # Warnings are expected: the stub has no benchmark series and no profile for every name.
    warnings = refresh_market_data(s, store)
    assert all(("profile data" in w) or ("BM:" in w) for w in warnings), warnings
    snap = build_snapshot(s, store)
    # only Acme is open: 1 share × 120 USD × 0.5 EUR/USD = 60 EUR ; cash 844.02
    assert len(snap.holdings) == 1
    h = snap.holdings[0]
    assert h.value_base == pytest.approx(60.0) and h.unrealized_base == pytest.approx(60.0 - 100.0)
    assert h.price_provenance.provider == "stub" and h.fx_provenance.provider == "fx"
    assert h.day_change_base == pytest.approx(1 * (120 - 120 / 1.1) * 0.5)
    assert snap.cash == pytest.approx(844.02) and snap.total_value == pytest.approx(904.02)
    assert h.weight == pytest.approx(60.0 / 904.02)
    assert not snap.history.empty and snap.history["total"].iloc[-1] == pytest.approx(904.02)
    assert snap.history["net_invested"].iloc[-1] == pytest.approx(900.0)
    assert h.issues == []
    assert any("Some Coin" in w and "valued at cost" in w for w in snap.warnings)
    assert snap.history.attrs["at_cost"]["COIN"] == ("2024-04-02", "2024-04-02")


def test_snapshot_without_prices_is_explicit(tmp_path):
    s = session_factory(make_engine(tmp_path / "t.db"))()
    import_bux_file(s, FIX, archive=False)

    class Dead(Prices):
        def resolve(self, *a, **k): return None
    store = MarketDataStore(s, MarketDataRouter([Dead()], [Fx()]))
    refresh_market_data(s, store)
    snap = build_snapshot(s, store)
    assert snap.total_value is None and snap.holdings[0].value_base is None
    assert any("no price" in w for w in snap.warnings)
    # history falls back to cost, explicitly flagged; the current value never does
    assert snap.history["total"].notna().all()
    assert any("Acme Inc" in w and "valued at cost" in w for w in snap.warnings)
