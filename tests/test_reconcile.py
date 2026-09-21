from decimal import Decimal as D
from pathlib import Path

import pytest

from bux_analyser.db import make_engine, session_factory
from bux_analyser.importers.persist import import_bux_file
from bux_analyser.marketdata.store import MarketDataRouter, MarketDataStore
from bux_analyser.reconcile import build_report, compare, parse_figures, parse_number, write_template
from bux_analyser.service import build_snapshot, refresh_market_data
from tests.test_service import Fx, Prices

FIX = Path(__file__).parent / "fixtures" / "bux_synthetic.csv"


@pytest.mark.parametrize("raw,want", [
    ("1234.56", D("1234.56")), ("1.234,56", D("1234.56")), ("1,234.56", D("1234.56")),
    ("€ 1 234,56", D("1234.56")), ("0,161834", D("0.161834")), ("-12,5", D("-12.5")),
    ("1,234", D("1234")), ("", None), ("  ", None), ("n/a", None), ("110", D("110")),
])
def test_parse_number_handles_both_decimal_conventions(raw, want):
    assert parse_number(raw) == want


def test_parse_figures_accepts_semicolons_comments_and_specials():
    text = (
        "# my figures\n"
        "isin;name;quantity;avg_price;value_eur\n"
        "US0000000001;Acme;1;110,00;60,00\n"
        "NL0000000002;Dutch NV;;;\n"
        "CASH;cash;;;844,02\n"
        "TOTAL;total;;;904,02\n"
        "AS_OF;2026-09-21;;;\n"
    )
    fig = parse_figures(text)
    assert fig.holdings == {"US0000000001": {"quantity": D("1"), "avg_price": D("110.00"), "value": D("60.00")}}
    assert fig.cash == D("844.02") and fig.total == D("904.02") and fig.as_of == "2026-09-21"
    assert not fig.problems


def _snapshot(tmp_path):
    s = session_factory(make_engine(tmp_path / "t.db"))()
    import_bux_file(s, FIX, archive=False)
    store = MarketDataStore(s, MarketDataRouter([Prices()], [Fx()]))
    refresh_market_data(s, store)
    return s, store, build_snapshot(s, store)


def test_report_contains_every_section(tmp_path):
    s, store, snap = _snapshot(tmp_path)
    r = build_report(s, snap, store)
    for section in ["[1] IMPORT", "[2] MARKET DATA PROVIDERS", "[3] SYMBOL RESOLUTION",
                    "[4] HOLDINGS", "[5] TOTALS", "[6] COMPARISON"]:
        assert section in r
    assert "Acme Inc" in r and "US0000000001" in r
    assert "17 transactions" in r and "--template" in r


def test_matching_figures_report_no_problems(tmp_path):
    s, store, snap = _snapshot(tmp_path)
    fig = parse_figures("isin,name,quantity,avg_price,value_eur\n"
                        "US0000000001,Acme,1,110.00,60.00\nCASH,,,,844.02\nTOTAL,,,,904.02\n")
    lines, problems = compare(snap, fig)
    assert problems == 0
    text = "\n".join(lines)
    assert "cash:" in text and "OK" in text
    assert "everything checked matches" in build_report(s, snap, store, fig)


def test_mismatches_are_named_precisely(tmp_path):
    s, store, snap = _snapshot(tmp_path)
    fig = parse_figures("isin,name,quantity,avg_price,value_eur\n"
                        "US0000000001,Acme,2,109.00,60.00\n"
                        "XX9999999999,Ghost,5,10.00,\n"
                        "CASH,,,,800.00\n")
    lines, problems = compare(snap, fig)
    text = "\n".join(lines)
    assert problems == 4  # quantity, average price, holding missing from our data, cash
    assert "qty ours 1" in text and "BUX 2" in text
    assert "avg ours 110.0000 vs BUX 109.0000" in text
    assert "XX9999999999" in text and "not held" in text
    assert "cash:" in text and "MISMATCH" in text


def test_value_difference_is_reported_but_not_counted(tmp_path):
    """A live-price difference is informational; ledger figures must match exactly."""
    s, store, snap = _snapshot(tmp_path)
    lines, problems = compare(snap, parse_figures("isin,quantity,avg_price,value_eur\nUS0000000001,1,110.00,75.00\n"))
    assert problems == 0
    assert "value ours 60.00 vs BUX 75.00" in "\n".join(lines)


def test_price_tolerance_absorbs_display_rounding(tmp_path):
    s, store, snap = _snapshot(tmp_path)
    # BUX displays 110.00; a 0.004 difference must not be flagged
    _, problems = compare(snap, parse_figures("isin,quantity,avg_price\nUS0000000001,1,110.004\n"))
    assert problems == 0


def test_template_lists_our_holdings(tmp_path):
    s, store, snap = _snapshot(tmp_path)
    p = write_template(snap, tmp_path / "fig.csv")
    text = p.read_text()
    assert "US0000000001,Acme Inc,,," in text and "CASH," in text and "TOTAL," in text
    assert parse_figures(text).holdings == {}  # blank template checks nothing
