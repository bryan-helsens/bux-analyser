"""Fundamentals: parsing filings, deriving ratios, and refusing to guess."""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from bux_analyser.analytics import fundamentals as fa
from bux_analyser.marketdata.edgar import EdgarProvider, parse_company_facts
from bux_analyser.marketdata.fundamentals import FY, Q, TTM, FinancialPeriod, Fundamentals
from bux_analyser.marketdata.yahoo import parse_yahoo_statements

FIX = json.loads((Path(__file__).parent / "fixtures" / "edgar_companyfacts.json").read_text())


@pytest.fixture
def edgar() -> Fundamentals:
    return parse_company_facts(FIX, "US0000000001", "FIXT")


def test_edgar_parsing_keeps_filing_dates(edgar):
    fy2024 = next(p for p in edgar.sorted_periods(FY) if p.period_end == date(2024, 12, 31))
    assert fy2024.as_reported_at == date(2025, 2, 11)
    assert fy2024.get("revenue") == 1500.0 and fy2024.get("net_income") == 200.0
    assert edgar.currency == "USD"


def test_edgar_ignores_restatements_and_non_periodic_forms(edgar):
    fy2024 = next(p for p in edgar.sorted_periods(FY) if p.period_end == date(2024, 12, 31))
    assert fy2024.get("revenue") == 1500.0          # not the 1490 restated in 2026
    assert not any(p.period_end == date(2023, 11, 30) for p in edgar.periods)   # the 8-K


def test_free_cash_flow_handles_either_capex_sign():
    p = FinancialPeriod(date(2024, 12, 31), FY, {"operating_cash_flow": 280.0, "capex": 80.0})
    q = FinancialPeriod(date(2024, 12, 31), FY, {"operating_cash_flow": 280.0, "capex": -80.0})
    assert p.free_cash_flow == 200.0 and q.free_cash_flow == 200.0
    assert FinancialPeriod(date(2024, 12, 31), FY, {"capex": 80.0}).free_cash_flow is None


def test_point_in_time_hides_figures_not_yet_filed(edgar):
    # On 1 Jan 2025 the 2024 annual report had not been filed yet
    assert edgar.latest(FY, as_of=date(2025, 1, 1)).period_end == date(2023, 12, 31)
    assert edgar.latest(FY, as_of=date(2025, 3, 1)).period_end == date(2024, 12, 31)
    assert edgar.latest(FY, as_of=date(2020, 1, 1)) is None
    series = edgar.annual_series("revenue", as_of=date(2025, 1, 1))
    assert [v for _, v in series] == [1000.0, 1200.0]


def test_trailing_twelve_months_sums_flows_and_carries_balances_forward(edgar):
    ttm = edgar.trailing_twelve_months()
    assert ttm.period_type == TTM and ttm.period_end == date(2025, 12, 31)
    assert ttm.get("revenue") == pytest.approx(400 + 420 + 430 + 450)
    assert ttm.get("net_income") == pytest.approx(50 + 55 + 60 + 65)
    assert ttm.free_cash_flow == pytest.approx(280 - 80)
    # quarterly filings omit these; they must carry forward from the annual report
    assert ttm.get("total_equity") == 900.0 and ttm.get("shares_diluted") == 100.0


def test_trailing_twelve_months_falls_back_to_the_annual_report():
    annual_only = Fundamentals("X", periods=[
        FinancialPeriod(date(2024, 12, 31), FY, {"revenue": 100.0}, date(2025, 2, 1))])
    assert annual_only.trailing_twelve_months().period_type == FY


def test_metrics_derive_valuation_growth_and_quality(edgar):
    m = fa.compute(edgar, market_cap=4000.0, as_of=date(2026, 3, 1),
                   as_reported_by=date(2025, 6, 1))
    assert m.period_end == date(2024, 12, 31)
    assert m.get("price_to_earnings") == pytest.approx(4000 / 200)
    assert m.get("price_to_sales") == pytest.approx(4000 / 1500)
    assert m.get("price_to_book") == pytest.approx(4000 / 900)
    assert m.get("fcf_yield") == pytest.approx(200 / 4000)
    assert m.get("ev_to_ebit") == pytest.approx((4000 + 250) / 300)      # net debt 400 - 150
    assert m.get("gross_margin") == pytest.approx(0.4)
    assert m.get("return_on_equity") == pytest.approx(200 / 900)
    assert m.get("fcf_conversion") == pytest.approx(200 / 200)
    assert m.get("revenue_cagr_3y") == pytest.approx((1500 / 1000) ** (1 / 2.0) - 1, rel=0.02)


def test_metrics_refuse_meaningless_ratios():
    loss_making = Fundamentals("X", currency="EUR", periods=[FinancialPeriod(
        date(2024, 12, 31), FY, {"revenue": 100.0, "net_income": -50.0, "total_equity": -20.0},
        date(2025, 2, 1))])
    m = fa.compute(loss_making, market_cap=1000.0, as_of=date(2025, 6, 1))
    assert m.get("price_to_earnings") is None      # a negative P/E would mislead
    assert m.get("price_to_book") is None
    assert m.get("return_on_equity") is None
    assert m.get("net_margin") == pytest.approx(-0.5)   # a negative margin is meaningful


def test_growth_refuses_to_measure_from_a_loss():
    f = Fundamentals("X", periods=[
        FinancialPeriod(date(2023, 12, 31), FY, {"net_income": -10.0}, date(2024, 2, 1)),
        FinancialPeriod(date(2024, 12, 31), FY, {"net_income": 50.0}, date(2025, 2, 1))])
    m = fa.compute(f, market_cap=None, as_of=date(2025, 6, 1))
    assert m.get("earnings_cagr_3y") is None


def test_stale_figures_are_called_out(edgar):
    m = fa.compute(edgar, market_cap=4000.0, as_of=date(2027, 6, 1), as_reported_by=date(2025, 6, 1))
    assert any("no longer describe" in n for n in m.notes)
    assert m.stale_days > 400


def test_unverified_sources_are_marked_as_such():
    f = Fundamentals("X", periods=[FinancialPeriod(
        date(2024, 12, 31), FY, {"revenue": 100.0, "net_income": 10.0}, None,
        quality="unverified")])
    m = fa.compute(f, market_cap=100.0, as_of=date(2025, 6, 1))
    assert m.quality == "unverified" and any("indicative" in n for n in m.notes)
    assert m.reported_at is None


def test_yahoo_statement_parsing_folds_three_frames():
    cols = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    income = pd.DataFrame({cols[0]: [1500.0, 600.0, 200.0, 2.0], cols[1]: [1200.0, 480.0, 120.0, 1.2]},
                          index=["Total Revenue", "Gross Profit", "Net Income", "Diluted EPS"])
    balance = pd.DataFrame({cols[0]: [900.0, 2000.0], cols[1]: [800.0, 1800.0]},
                           index=["Stockholders Equity", "Total Assets"])
    cash = pd.DataFrame({cols[0]: [280.0, -80.0], cols[1]: [240.0, -70.0]},
                        index=["Operating Cash Flow", "Capital Expenditure"])
    periods = parse_yahoo_statements([income, balance, cash], FY, "USD", "Yahoo")
    assert len(periods) == 2
    latest = max(periods, key=lambda p: p.period_end)
    assert latest.get("revenue") == 1500.0 and latest.get("total_equity") == 900.0
    assert latest.free_cash_flow == 200.0
    assert latest.quality == "unverified" and latest.as_reported_at is None


def test_yahoo_parsing_tolerates_missing_frames():
    assert parse_yahoo_statements([None, pd.DataFrame(), None], FY, "EUR", "Yahoo") == []


def test_edgar_strips_exchange_suffixes_when_looking_up_a_cik():
    provider = EdgarProvider()
    provider._cik_map = {"AAPL": 320193}
    assert provider.cik_for("AAPL.AS") == 320193
    assert provider.cik_for("ASML.AS") is None
    assert provider.cik_for(None) is None
