from datetime import date
from decimal import Decimal as D

import pandas as pd
import pytest

from bux_analyser.core.performance import portfolio_xirr, time_weighted_return, valuation_history, xirr
from bux_analyser.core.types import Txn, TxnType as T


def tx(i, type, d, **kw):
    base = dict(currency="EUR", fx_rate=D("1"))
    base.update(kw)
    return Txn(id=f"t{i}", type=type, date=date(*d), **base)


def test_xirr_known_values():
    # invest 1000, get 1100 one year later → 10%
    assert xirr([(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 1100.0)]) == pytest.approx(0.10, abs=1e-3)  # 2024 is a leap year (366/365)
    # two deposits, one terminal value
    r = xirr([(date(2024, 1, 1), -1000.0), (date(2024, 7, 1), -1000.0), (date(2025, 1, 1), 2100.0)])
    assert 0.06 < r < 0.07
    assert xirr([(date(2024, 1, 1), -1.0)]) is None
    assert xirr([(date(2024, 1, 1), -1.0), (date(2024, 2, 1), -1.0)]) is None


def test_valuation_history_and_twr_with_flows():
    txns = [
        tx(1, T.DEPOSIT, (2024, 1, 1), amount=D("1000")),
        tx(2, T.BUY, (2024, 1, 1), isin="X", quantity=D("10"), price=D("100")),
        tx(3, T.DEPOSIT, (2024, 1, 3), amount=D("500")),          # flow while price doubled
        tx(4, T.BUY, (2024, 1, 3), isin="X", quantity=D("2.5"), price=D("200")),
    ]
    idx = pd.date_range("2024-01-01", "2024-01-04")
    prices = {"X": pd.Series([100.0, 150.0, 200.0, 200.0], index=idx)}
    h = valuation_history(txns, prices, {}, {"X": "EUR"}, end=date(2024, 1, 4))
    assert list(h["total"].round(6)) == [1000.0, 1500.0, 2500.0, 2500.0]
    assert list(h["cash"]) == [0.0, 0.0, 0.0, 0.0]
    assert list(h["net_invested"]) == [1000.0, 1000.0, 1500.0, 1500.0]
    twr = time_weighted_return(h["total"], h["external_flow"])
    # day2: 1500/1000 = 1.5 ; day3: (2500−500)/1500 = 1.3333 ; day4: flat → 2.0 total
    assert twr.iloc[-1] == pytest.approx(2.0)
    assert portfolio_xirr(h) is None  # < 30 days: annualising is meaningless


def test_portfolio_xirr_over_one_year():
    txns = [tx(1, T.DEPOSIT, (2024, 1, 1), amount=D("1000")),
            tx(2, T.BUY, (2024, 1, 1), isin="X", quantity=D("10"), price=D("100"))]
    idx = pd.date_range("2024-01-01", "2025-01-01")
    prices = {"X": pd.Series([100.0] * (len(idx) - 1) + [110.0], index=idx)}
    h = valuation_history(txns, prices, {}, {"X": "EUR"}, end=date(2025, 1, 1))
    assert portfolio_xirr(h) == pytest.approx(0.10, abs=1e-3)


def test_multicurrency_valuation_uses_fx_and_cash_in_base():
    txns = [
        tx(1, T.DEPOSIT, (2024, 1, 1), amount=D("1000")),
        tx(2, T.BUY, (2024, 1, 1), isin="U", currency="USD", fx_rate=D("0.9"), quantity=D("10"), price=D("50")),
    ]
    idx = pd.date_range("2024-01-01", "2024-01-02")
    prices = {"U": pd.Series([50.0, 60.0], index=idx)}
    fx = {"USD": pd.Series([0.9, 1.0], index=idx)}
    h = valuation_history(txns, prices, fx, {"U": "USD"}, end=date(2024, 1, 2))
    assert h["cash"].iloc[0] == pytest.approx(1000 - 500 * 0.9)
    assert h["securities_value"].iloc[0] == pytest.approx(500 * 0.9)
    assert h["securities_value"].iloc[1] == pytest.approx(600 * 1.0)


def test_missing_price_never_fabricates():
    txns = [tx(1, T.BUY, (2024, 1, 1), isin="X", quantity=D("1"), price=D("10")),
            tx(2, T.SELL, (2024, 1, 2), isin="X", quantity=D("1"), price=D("10"))]
    h = valuation_history(txns, {}, {}, {"X": "EUR"}, end=date(2024, 1, 3))
    assert h["total"].isna().iloc[0] and not h["total"].isna().iloc[2]  # unknown only while held
    assert portfolio_xirr(h) is None
    h2 = valuation_history(txns, {}, {}, {"X": "EUR"}, end=date(2024, 1, 3), at_cost={"X": 10.0})
    assert h2["total"].iloc[0] == 0.0 and h2.attrs["at_cost"] == {"X": ("2024-01-01", "2024-01-01")}
