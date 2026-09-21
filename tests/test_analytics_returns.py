from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from bux_analyser.analytics.returns import (Decomposition, aggregate_decomposition, annualise,
                                            average_applied_fx, contribution_to_return, decompose,
                                            monthly_table, period_returns)


def test_decomposition_is_exact_against_value_over_cost():
    """The split must reproduce the position's actual EUR return, with no residual."""
    qty, avg_cost_local, avg_fx = 10.0, 100.0, 0.90     # bought at 100 USD, 0.90 EUR/USD
    price_now, fx_now = 130.0, 0.85                     # price up 30%, dollar down
    d = decompose(price_now, avg_cost_local, fx_now, avg_fx, "USD", "EUR")
    cost_base = qty * avg_cost_local * avg_fx
    value_base = qty * price_now * fx_now
    assert d.local == pytest.approx(0.30)
    assert d.fx == pytest.approx(-0.055555, abs=1e-6)
    assert d.total == pytest.approx(value_base / cost_base - 1)
    assert d.total == pytest.approx(d.local + d.fx + d.cross)   # cross term closes the gap
    assert not d.is_single_currency


def test_decomposition_for_a_euro_holding_has_no_currency_effect():
    d = decompose(120.0, 100.0, 1.0, 1.0, "EUR", "EUR")
    assert d.local == pytest.approx(0.2) and d.fx == 0.0 and d.total == pytest.approx(0.2)
    assert d.is_single_currency


def test_average_applied_fx_recovers_the_rate_actually_paid():
    # 2 shares at 110 USD, cash leg 200 EUR -> effective 200/220
    assert average_applied_fx(D("200"), D("220")) == pytest.approx(200 / 220)
    assert average_applied_fx(D("0"), D("0")) is None


def test_decompose_refuses_impossible_inputs():
    assert decompose(100.0, 0.0, 1.0, 1.0) is None
    assert decompose(None, 100.0, 1.0, 1.0) is None
    assert decompose(100.0, 100.0, None, 1.0) is None


def test_aggregate_is_cost_weighted_and_reconciles():
    a = decompose(130.0, 100.0, 0.85, 0.90, "USD")      # cost weight 900
    b = decompose(110.0, 100.0, 1.0, 1.0, "EUR")        # cost weight 100
    agg = aggregate_decomposition([(a, 900.0), (b, 100.0)])
    assert agg.total == pytest.approx((a.total * 900 + b.total * 100) / 1000)
    assert agg.local_currency == "mixed"
    assert aggregate_decomposition([]) is None
    assert aggregate_decomposition([(None, 100.0)]) is None


def test_period_returns_compound_correctly():
    # A TWR index opens at 1.0 on its first day, then gains 10% in each of three months.
    idx = pd.to_datetime(["2024-01-01", "2024-01-31", "2024-02-29", "2024-03-31"])
    twr = pd.Series([1.0, 1.10, 1.21, 1.331], index=idx)
    m = period_returns(twr, "ME")
    assert list(np.round(m.to_numpy(), 6)) == [0.10, 0.10, 0.10]


def test_monthly_table_has_a_compounded_year_column():
    idx = pd.to_datetime(["2024-01-01"]).append(pd.date_range("2024-01-31", periods=12, freq="ME"))
    twr = pd.Series(np.concatenate([[1.0], np.cumprod(np.full(12, 1.01))]), index=idx)
    t = monthly_table(twr)
    assert list(t.index) == [2024] and "Jan" in t.columns and "Dec" in t.columns
    assert t.loc[2024, "Year"] == pytest.approx(1.01 ** 12 - 1, abs=1e-9)


def test_annualise_handles_short_and_long_windows():
    assert annualise(0.10, 365) == pytest.approx(0.10, abs=1e-3)
    assert annualise(0.21, 730) == pytest.approx(0.10, abs=2e-3)
    assert annualise(0.1, 0) is None
    assert annualise(-1.0, 365) is None


def test_contribution_to_return_weights_each_holding():
    w = pd.Series({"A": 0.5, "B": 0.5})
    r = pd.Series({"A": 0.20, "B": -0.10})
    c = contribution_to_return(w, r)
    assert c["A"] == pytest.approx(0.10) and c["B"] == pytest.approx(-0.05)
    assert list(c.index) == ["A", "B"]      # sorted by contribution
