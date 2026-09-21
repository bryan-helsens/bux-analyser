"""The dashboard rendered end to end: once with market data, once without.

Uses Streamlit's own test harness with stub providers, so it exercises the real
app script without touching the network.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import bux_analyser.db as db_module
from bux_analyser.db import make_engine, session_factory
from bux_analyser.importers.persist import import_bux_file
from bux_analyser.marketdata.base import PriceSeries, Provenance
from bux_analyser.marketdata.store import MarketDataRouter, MarketDataStore
from bux_analyser.service import refresh_market_data
from tests.test_service import Fx, Prices

APP = str(Path(__file__).parent.parent / "app.py")
FIX = Path(__file__).parent / "fixtures" / "bux_synthetic.csv"


class FullPrices(Prices):
    """Prices for the holdings and for the benchmarks, so every panel has data."""
    def eod_history(self, ids, start, end):
        if ids.isin.startswith("BM:"):
            idx = pd.date_range(start, end, freq="D")
            return PriceSeries(pd.Series(np.linspace(100.0, 112.0, len(idx)), index=idx), "EUR",
                               Provenance("stub", "Stub benchmark", Provenance.now(), end))
        if ids.isin not in self.levels:
            return None
        lvl, ccy = self.levels[ids.isin]
        idx = pd.date_range(start, end, freq="D")
        # a gently rising price so returns, drawdown and correlation all have something to chew on
        walk = lvl * (1 + 0.04 * np.sin(np.linspace(0, 8, len(idx))) + np.linspace(0, 0.1, len(idx)))
        return PriceSeries(pd.Series(walk, index=idx), ccy,
                           Provenance("stub", "Stub", Provenance.now(), end))


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    """A populated local database that the app script will open."""
    path = tmp_path / "app.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    session = session_factory(make_engine(path))()
    import_bux_file(session, FIX, archive=False)
    store = MarketDataStore(session, MarketDataRouter([FullPrices()], [Fx()]))
    refresh_market_data(session, store)
    session.commit()
    import streamlit as st
    st.cache_resource.clear()
    return path


def _run(timeout=120):
    at = AppTest.from_file(APP, default_timeout=timeout).run()
    assert not at.exception, [str(e.value)[:400] for e in at.exception]
    return at


def test_dashboard_renders_every_tab_with_market_data(app_db):
    at = _run()
    assert [t.label for t in at.tabs] == ["Overview", "Performance", "Exposure", "Risk",
                                          "Signals", "Scenarios", "Holdings", "Research",
                                          "Transactions"]
    labels = {m.label: m.value for m in at.metric}
    assert labels["Cash"] == "€ 844.02"
    assert labels["Total value"].startswith("€") and labels["Total value"] != "n/a"
    assert labels["Total P/L"] != "n/a"
    assert labels["Time-weighted return"].endswith("%")
    # risk panel populated
    assert "Volatility" in labels and labels["Volatility"] != "n/a"
    assert "Max drawdown" in labels
    # exposure panel populated
    assert "Largest holding" in labels and labels["Largest holding"] != "n/a"


def test_dashboard_shows_holdings_and_transactions(app_db):
    at = _run()
    frames = [d.value for d in at.dataframe]
    assert any("ISIN" in f.columns for f in frames), "holdings table missing"
    tx = next(f for f in frames if "Cash effect €" in f.columns)
    assert len(tx) == 17 and set(tx["Type"]) >= {"buy", "sell", "dividend", "fee", "deposit"}


def test_signals_and_scenarios_populate_for_a_rankable_portfolio(tmp_path, monkeypatch):
    """Four holdings is enough to rank, so scores, recommendations and a simulation appear."""
    from tests.test_intelligence import MultiPrices
    path = tmp_path / "multi.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    session = session_factory(make_engine(path))()
    import_bux_file(session, Path(__file__).parent / "fixtures" / "bux_multi.csv", archive=False)
    store = MarketDataStore(session, MarketDataRouter([MultiPrices()], [Fx()]))
    refresh_market_data(session, store)
    session.commit()
    import streamlit as st
    st.cache_resource.clear()

    at = _run()
    frames = [d.value for d in at.dataframe]
    scores = next((f for f in frames if "Overall" in f.columns and "Signal" in f.columns), None)
    assert scores is not None and len(scores) == 4
    assert scores["Momentum"].notna().any()
    assert scores["Valuation"].isna().all()        # no fundamentals: the column stays empty

    labels = {m.label: m.value for m in at.metric}
    assert "Median outcome" in labels and labels["Median outcome"].startswith("€")
    assert "Chance of ending below money in" in labels
    captions = " ".join(c.value for c in at.caption)
    assert "not a forecast" in captions
    assert "would have done" in captions          # the what-if table states its limits
    assert any("fundamentals" in i.value for i in at.info)
    # the Research tab answers a question without any model involved
    captions = " ".join(c.value for c in at.caption)
    assert "not by a language model" in captions


def test_dashboard_without_market_data_says_so_instead_of_failing(tmp_path, monkeypatch):
    path = tmp_path / "bare.db"
    monkeypatch.setattr(db_module, "DB_PATH", path)
    session = session_factory(make_engine(path))()
    import_bux_file(session, FIX, archive=False)
    import streamlit as st
    st.cache_resource.clear()
    at = _run()
    labels = {m.label: m.value for m in at.metric}
    assert labels["Total value"] == "n/a" and labels["Cash"] == "€ 844.02"
    assert any("no price" in w.value.lower() or "benchmark" in w.value.lower() for w in at.warning)


def test_empty_database_invites_an_import(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "empty.db")
    import streamlit as st
    st.cache_resource.clear()
    at = _run()
    assert any("Import a BUX transaction-history CSV" in i.value for i in at.info)
    assert not at.tabs
