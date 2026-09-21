"""Phase 2 analytics as they reach the dashboard, end to end from an import."""
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bux_analyser.analytics.portfolio import ETF_BUCKET, build_exposures, eur_price_frame
from bux_analyser.db import make_engine, session_factory
from bux_analyser.importers.persist import import_bux_file
from bux_analyser.marketdata.base import FxSeries, PriceSeries, Provenance
from bux_analyser.marketdata.store import MarketDataRouter, MarketDataStore
from bux_analyser.service import build_snapshot, refresh_market_data
from tests.test_service import Fx, Prices

FIX = Path(__file__).parent / "fixtures" / "bux_synthetic.csv"


class BenchPrices(Prices):
    """Adds a benchmark series so comparisons can be exercised."""
    def resolve(self, isin, hint_name=None, hint_currency=None):
        return super().resolve(isin, hint_name, hint_currency)

    def eod_history(self, ids, start, end):
        if ids.isin == "BM:IWDA.AS":
            idx = pd.date_range(start, end, freq="D")
            return PriceSeries(pd.Series(np.linspace(100.0, 110.0, len(idx)), index=idx), "EUR",
                               Provenance("stub", "Stub", Provenance.now(), end))
        return super().eod_history(ids, start, end)


def _snap(tmp_path, provider=None):
    s = session_factory(make_engine(tmp_path / "t.db"))()
    import_bux_file(s, FIX, archive=False)
    store = MarketDataStore(s, MarketDataRouter([provider or Prices()], [Fx()]))
    refresh_market_data(s, store)
    return build_snapshot(s, store)


def test_eur_price_frame_converts_before_differencing():
    idx = pd.date_range("2024-01-01", periods=3)
    prices = {"U": pd.Series([100.0, 100.0, 100.0], index=idx)}
    fx = {"USD": pd.Series([0.90, 0.95, 1.00], index=idx)}
    eur = eur_price_frame(prices, fx, {"U": "USD"}, idx)
    assert list(eur["U"]) == [90.0, 95.0, 100.0]
    # a flat dollar price still produces a real EUR return for a euro investor
    assert eur["U"].pct_change().iloc[1] == pytest.approx(95 / 90 - 1)


def test_eur_price_frame_omits_holdings_without_a_rate():
    idx = pd.date_range("2024-01-01", periods=3)
    eur = eur_price_frame({"U": pd.Series([1.0] * 3, index=idx)}, {}, {"U": "USD"}, idx)
    assert "U" not in eur.columns


def test_exposures_bucket_etfs_instead_of_inventing_sectors():
    w = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    meta = {"A": {"asset_type": "stock", "sector": "Technology", "country": "United States", "currency": "USD"},
            "B": {"asset_type": "etf", "sector": None, "country": None, "currency": "EUR"},
            "C": {"asset_type": "stock", "sector": None, "country": None, "currency": "EUR"}}
    ex = build_exposures(w, meta)
    assert ex["sector"].weights[ETF_BUCKET] == pytest.approx(0.3)
    assert ex["sector"].weights["Technology"] == pytest.approx(0.5)
    assert ex["sector"].unknown_weight == pytest.approx(0.2)   # C has no sector: shown as unknown
    assert ex["currency"].weights["EUR"] == pytest.approx(0.5)
    assert ex["asset_type"].weights["stock"] == pytest.approx(0.7)
    assert "not the currency of its holdings" in ex["currency"].note


def test_snapshot_carries_analytics_with_decomposition(tmp_path):
    snap = _snap(tmp_path)
    a = snap.analytics
    assert a is not None
    h = snap.holdings[0]                       # Acme: bought 2 @110 USD for 200 EUR, now 120 USD @ 0.5
    d = h.decomposition
    assert d.local == pytest.approx(120 / 110 - 1)
    assert d.fx == pytest.approx(0.5 / (200 / 220) - 1)
    assert d.total == pytest.approx(h.value_base / float(h.cost_base) - 1)
    assert a.decomposition.total == pytest.approx(d.total)     # single holding: parts equal the whole


def test_snapshot_exposures_and_concentration(tmp_path):
    snap = _snap(tmp_path)
    a = snap.analytics
    assert a.concentration.n == 1 and a.concentration.top1 == pytest.approx(1.0)
    assert a.exposures["sector"].weights["Technology"] == pytest.approx(1.0)
    assert a.exposures["currency"].weights["USD"] == pytest.approx(1.0)


def test_benchmark_comparison_is_built_when_history_exists(tmp_path):
    snap = _snap(tmp_path, BenchPrices())
    labels = [b.label for b in snap.analytics.benchmarks]
    assert "MSCI World" in labels
    bm = next(b for b in snap.analytics.benchmarks if b.label == "MSCI World")
    assert bm.index.iloc[0] == pytest.approx(1.0)
    assert bm.total_return is not None and bm.total_return_basis is True
    assert bm.excess is not None


def test_analytics_degrade_without_prices(tmp_path):
    class Dead(Prices):
        def resolve(self, *a, **k): return None
    snap = _snap(tmp_path, Dead())
    a = snap.analytics
    assert a.exposures == {} and a.concentration is None
    assert a.correlation.empty and a.risk_contribution.empty
    assert a.benchmarks == []
    assert any("benchmark" in w.lower() for w in snap.warnings)
