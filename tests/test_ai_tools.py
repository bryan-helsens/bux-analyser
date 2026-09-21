"""The assistant's query layer. It must answer correctly with no model present,
and must never let a caller reach past the catalogue."""
from pathlib import Path

import pandas as pd
import pytest

from bux_analyser import intelligence
from bux_analyser.ai.tools import SYSTEM_PROMPT, PortfolioTools, ToolResult
from tests.test_intelligence import MultiPrices, _snapshot_with_store


@pytest.fixture
def tools(tmp_path):
    session, store, snapshot = _snapshot_with_store(tmp_path)
    intel = intelligence.build(session, snapshot, store=store)
    return PortfolioTools(snapshot, intel)


def test_portfolio_summary_reports_real_figures(tools):
    r = tools.get_portfolio_summary()
    assert r.data["holdings"] == 4
    assert r.data["cash_eur"] == pytest.approx(tools.snapshot.cash)
    assert "worth EUR" in r.summary and "put in" in r.summary
    assert r.sources


def test_position_lookup_is_forgiving_but_exact_when_it_can_be(tools):
    by_name = tools.get_position("Acme Inc")
    by_isin = tools.get_position("US0000000001")
    by_partial = tools.get_position("acme")
    assert by_name.data["isin"] == by_isin.data["isin"] == by_partial.data["isin"]
    assert by_name.data["quantity"] == pytest.approx(20.0)


def test_an_unknown_holding_lists_what_is_available_instead_of_failing(tools):
    r = tools.get_position("Tesla")
    assert "No holding matches" in r.summary and "Acme Inc" in r.summary
    assert r.data == {}


def test_currency_effect_is_explained_for_a_foreign_holding(tools):
    usd = tools.get_position("Acme Inc")            # bought in USD
    eur = tools.get_position("Dutch NV")            # bought in EUR
    assert "exchange rate" in usd.summary
    assert "none of the return came from currency" in eur.summary


def test_allocation_names_what_it_could_not_classify(tools):
    r = tools.get_allocation("sector")
    assert r.table is not None and not r.table.empty
    assert any("fund" in c.lower() or "sector" in c.lower() for c in r.caveats)
    unknown = tools.get_allocation("phase_of_the_moon")
    assert "No breakdown by" in unknown.summary


def test_risk_and_contributors_report_or_explain_their_absence(tools):
    risk = tools.get_risk_metrics()
    assert risk.data.get("observations", 0) > 0 or "need price history" in risk.summary
    contributors = tools.get_risk_contributors()
    assert contributors.table is not None or "shared price history" in contributors.summary


def test_score_and_signal_carry_their_limits(tools):
    score = tools.get_score("Acme Inc")
    assert "compare your own holdings" in " ".join(score.caveats)
    signal = tools.get_recommendation_reasons("Acme Inc")
    assert any("not been tested" in c for c in signal.caveats)


def test_fundamentals_say_plainly_when_nothing_is_cached(tools):
    r = tools.get_fundamentals("Acme Inc")
    assert "No financial statements are cached" in r.summary
    assert r.data == {}


def test_dispatch_refuses_anything_outside_the_catalogue(tools):
    assert "not an available function" in tools.call("drop_all_tables").summary
    assert "not an available function" in tools.call("_find", query="x").summary
    assert tools.call("get_portfolio_summary").data["holdings"] == 4
    assert "Wrong arguments" in tools.call("get_position").summary


def test_every_catalogued_function_exists_and_returns_a_result(tools):
    for spec in tools.catalogue():
        assert hasattr(tools, spec["name"]), spec["name"]
        kwargs = {}
        if "query" in spec["parameters"]:
            kwargs["query"] = "Acme Inc"
        result = tools.call(spec["name"], **kwargs)
        assert isinstance(result, ToolResult) and result.summary


def test_prompt_context_carries_caveats_alongside_numbers(tools):
    text = tools.get_score("Acme Inc").as_prompt_context()
    assert "[get_score]" in text and "caveat:" in text and "sources:" in text


def test_the_system_prompt_forbids_inventing_numbers():
    assert "never calculate, estimate or recall a number yourself" in SYSTEM_PROMPT.lower()
    assert "price target" in SYSTEM_PROMPT.lower()
