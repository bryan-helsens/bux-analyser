"""The briefing is the only thing that leaves the machine, so what it does and does not
contain is the whole safety story."""
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from bux_analyser import intelligence
from bux_analyser.ai.briefing import QUESTIONS, RULES, BriefingOptions, build
from bux_analyser.db import FundamentalsCache
from bux_analyser.marketdata.edgar import parse_company_facts
from bux_analyser.marketdata.fundamentals import to_json
from tests.test_intelligence import _snapshot_with_store

FACTS = json.loads((Path(__file__).parent / "fixtures" / "edgar_companyfacts.json").read_text())


@pytest.fixture
def portfolio(tmp_path):
    session, store, snapshot = _snapshot_with_store(tmp_path)
    for isin in ("US0000000001", "NL0000000002"):
        f = parse_company_facts(FACTS, isin, "FIXT")
        session.add(FundamentalsCache(isin=isin, provider="edgar", payload=json.dumps(to_json(f)),
                                      currency="USD", quality="reported",
                                      latest_report=date(2026, 2, 10),
                                      retrieved_at=datetime.now(timezone.utc)))
    session.commit()
    intel = intelligence.build(session, snapshot, store=store)
    return snapshot, intel


def test_the_briefing_opens_with_rules_that_forbid_inventing_numbers(portfolio):
    snapshot, intel = portfolio
    b = build(snapshot, intel)
    assert b.markdown.startswith("# Portfolio briefing")
    assert RULES in b.markdown
    assert "Do not calculate, estimate, or recall any" in b.markdown
    assert "price targets, forecasts" in b.markdown


def test_amounts_are_withheld_by_default(portfolio):
    snapshot, intel = portfolio
    b = build(snapshot, intel)
    assert b.redacted
    assert "Amounts are withheld" in b.markdown
    assert "do not try to infer the portfolio's size" in b.markdown
    # the actual cash figure must not appear anywhere
    assert f"{snapshot.cash:,.2f}" not in b.markdown
    assert "Value €" not in b.markdown


def test_amounts_appear_only_when_asked_for(portfolio):
    snapshot, intel = portfolio
    b = build(snapshot, intel, BriefingOptions(include_amounts=True))
    assert not b.redacted
    assert "Total value: €" in b.markdown and "Value €" in b.markdown
    assert "Amounts are withheld" not in b.markdown


def test_returns_and_weights_survive_redaction(portfolio):
    """Withholding amounts must not cost the analysis: proportions are what matter."""
    snapshot, intel = portfolio
    b = build(snapshot, intel)
    assert "Weight" in b.markdown and "From currency" in b.markdown
    assert "Time-weighted return" in b.markdown
    assert "Total return on capital put in" in b.markdown


def test_every_section_appears_and_can_be_switched_off(portfolio):
    snapshot, intel = portfolio
    full = build(snapshot, intel)
    assert {"Holdings", "Performance", "Risk", "Exposure", "Scores", "Fundamentals",
            "Alerts"} <= set(full.sections)
    trimmed = build(snapshot, intel, BriefingOptions(
        include_holdings=False, include_risk=False, include_fundamentals=False,
        include_alerts=False, include_scores=False, include_exposure=False,
        include_performance=False))
    assert trimmed.sections == ["Portfolio"]
    assert len(trimmed.markdown) < len(full.markdown)


def test_the_limitations_section_names_real_gaps(portfolio):
    snapshot, intel = portfolio
    b = build(snapshot, intel)
    limits = b.markdown.split("## What this briefing cannot tell you")[1]
    assert "Nothing here forecasts anything" in limits
    assert "never been tested against subsequent returns" in limits
    assert "no news" in limits.lower()
    # two of four holdings have no statements: the briefing must name them
    assert "No fundamentals for:" in limits


def test_a_portfolio_without_fundamentals_says_so_loudly(tmp_path):
    session, store, snapshot = _snapshot_with_store(tmp_path)
    intel = intelligence.build(session, snapshot, store=store)
    b = build(snapshot, intel)
    assert "No financial statements are cached for any holding" in b.markdown
    assert "whether a holding is expensively or cheaply valued" in b.markdown


def test_price_only_benchmarks_are_flagged_where_they_appear(portfolio):
    snapshot, intel = portfolio
    b = build(snapshot, intel)
    if "price only" in b.markdown:
        assert "understates the benchmark" in b.markdown


def test_the_question_is_appended_verbatim(portfolio):
    snapshot, intel = portfolio
    question = QUESTIONS["Biggest risks"]
    b = build(snapshot, intel, BriefingOptions(question=question))
    assert b.markdown.rstrip().endswith(question)
    assert "## My question" in b.markdown


def test_question_presets_are_open_ended_not_leading():
    joined = " ".join(QUESTIONS.values()).lower()
    assert "should i buy" not in joined and "should i sell" not in joined
    assert "predict" not in joined
    assert "challenge me" in " ".join(QUESTIONS).lower()


def test_size_is_reported_so_a_long_briefing_can_be_trimmed(portfolio):
    snapshot, intel = portfolio
    b = build(snapshot, intel)
    assert b.characters > 500 and b.approx_tokens == len(b.markdown) // 4


def test_a_briefing_can_be_built_without_the_intelligence_layer(portfolio):
    snapshot, _ = portfolio
    b = build(snapshot, None)
    assert "# Portfolio briefing" in b.markdown
    assert "Scores" not in b.sections and "Alerts" not in b.sections
