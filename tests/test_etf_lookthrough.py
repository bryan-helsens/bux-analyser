"""Issuer holdings files and the look-through they make possible."""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from bux_analyser.analytics.portfolio import ETF_BUCKET, UNKNOWN, build_exposures
from bux_analyser.marketdata.etf_holdings import IssuerHoldingsProvider, parse_holdings_csv

FIXTURES = Path(__file__).parent / "fixtures"
ISHARES = (FIXTURES / "ishares_holdings.csv").read_text()
VANGUARD = (FIXTURES / "vanguard_holdings.csv").read_text()


def test_ishares_file_is_parsed_with_isins_not_tickers():
    h = parse_holdings_csv(ISHARES, "IE00B4L5Y983")
    assert h.as_of == date(2026, 6, 30)
    assert h.total_weight == pytest.approx(1.0) and h.is_complete
    apple = next(c for c in h.constituents if c.name == "APPLE INC")
    assert apple.identifier == "US0378331005" and apple.is_isin
    assert apple.weight == pytest.approx(0.40)     # 40.00% converted to a fraction
    assert apple.sector == "Information Technology" and apple.country == "United States"
    assert not h.notes


def test_a_different_issuer_layout_is_read_by_column_meaning():
    h = parse_holdings_csv(VANGUARD, "IE00B3RBWM25")
    assert h.as_of == date(2026, 5, 31)
    msft = h.constituents[0]
    assert msft.identifier == "MSFT" and not msft.is_isin
    assert msft.weight == pytest.approx(0.045)     # already a fraction, left alone
    assert any("does not publish ISINs" in n for n in h.notes)


def test_a_partial_file_says_it_is_partial():
    h = parse_holdings_csv(VANGUARD, "IE00B3RBWM25")
    assert not h.is_complete
    assert any("add to 8.0%" in n for n in h.notes)


def test_unparseable_input_returns_nothing_rather_than_guessing():
    assert parse_holdings_csv("", "X") is None
    assert parse_holdings_csv("just some prose\nwith no table at all", "X") is None
    assert parse_holdings_csv("a,b,c\n1,2,3", "X") is None


def test_provider_only_fetches_addresses_it_was_given():
    provider = IssuerHoldingsProvider(urls={})
    assert provider.holdings("IE00B3RBWM25") is None
    provider.register("IE00B3RBWM25", "https://example.invalid/holdings.csv")
    assert "IE00B3RBWM25" in provider.urls


# ---------------------------------------------------------------- look-through
def lookthrough_frame(text: str) -> pd.DataFrame:
    h = parse_holdings_csv(text, "ETF")
    return pd.DataFrame([{"weight": c.weight, "sector": c.sector, "country": c.country,
                          "currency": c.currency} for c in h.constituents])


def test_a_fund_is_dissolved_into_what_it_holds():
    weights = pd.Series({"STOCK": 0.5, "ETF": 0.5})
    meta = {"STOCK": {"asset_type": "stock", "sector": "Financials", "country": "Belgium",
                      "currency": "EUR"},
            "ETF": {"asset_type": "etf", "sector": None, "country": None, "currency": "EUR"}}
    ex = build_exposures(weights, meta, {"ETF": lookthrough_frame(ISHARES)})
    sector = ex["sector"].weights
    # the fund's 50% is spread: 60% of it is technology -> 30% of the portfolio
    assert sector["Information Technology"] == pytest.approx(0.30)
    assert sector["Financials"] == pytest.approx(0.50)
    assert ETF_BUCKET not in sector
    # Apple is 40% of the fund and its cash holding another 10%, both filed as US
    assert ex["country"].weights["United States"] == pytest.approx(0.25)
    assert ex["currency"].weights["CHF"] == pytest.approx(0.15)


def test_a_fund_without_holdings_stays_one_honest_bucket():
    weights = pd.Series({"STOCK": 0.5, "ETF": 0.5})
    meta = {"STOCK": {"asset_type": "stock", "sector": "Financials", "country": "Belgium",
                      "currency": "EUR"},
            "ETF": {"asset_type": "etf", "sector": None, "country": None, "currency": "EUR"}}
    ex = build_exposures(weights, meta, {})
    assert ex["sector"].weights[ETF_BUCKET] == pytest.approx(0.5)
    assert ex["sector"].unknown_weight == pytest.approx(0.5)
    assert "have not been loaded" in ex["sector"].note


def test_a_thinly_covered_file_is_not_trusted_to_describe_the_fund():
    """Vanguard's excerpt covers 8% of the fund. Spreading the other 92% across those
    three names would be a fabrication, so the fund keeps its own bucket."""
    weights = pd.Series({"ETF": 1.0})
    meta = {"ETF": {"asset_type": "etf", "sector": None, "country": None, "currency": "EUR"}}
    ex = build_exposures(weights, meta, {"ETF": lookthrough_frame(VANGUARD)})
    assert ex["sector"].weights[ETF_BUCKET] == pytest.approx(1.0)


def test_cash_inside_a_fund_shows_as_unclassified_not_as_a_sector():
    weights = pd.Series({"ETF": 1.0})
    meta = {"ETF": {"asset_type": "etf"}}
    frame = lookthrough_frame(ISHARES)
    frame.loc[frame["sector"] == "Cash and/or Derivatives", "sector"] = None
    ex = build_exposures(weights, meta, {"ETF": frame})
    assert ex["sector"].weights[UNKNOWN] == pytest.approx(0.10)
    assert ex["sector"].weights["Information Technology"] == pytest.approx(0.60)
