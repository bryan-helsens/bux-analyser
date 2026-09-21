from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from bux_analyser.alerts import (PORTFOLIO, SECURITY, AlertContext, Subject, acknowledge_all,
                                 evaluate, load_rules, record, recent_events, seed_default_rules)
from bux_analyser.db import AlertEvent, AlertRule, make_engine, session_factory

TODAY = date(2026, 9, 21)


@pytest.fixture
def session(tmp_path):
    return session_factory(make_engine(tmp_path / "a.db"))()


def rule(**kw):
    base = dict(label="test", scope=SECURITY, metric="day_change_pct", operator="lt",
                threshold=-0.05, enabled=True, cooldown_days=7, category="price", isin=None)
    base.update(kw)
    return AlertRule(**base)


def context(**metrics):
    return AlertContext(as_of=TODAY, securities={
        "A": Subject("A", "Acme", dict(metrics)),
        "B": Subject("B", "Beta Co", {"day_change_pct": 0.01, "weight": 0.05}),
    }, portfolio=Subject("PORTFOLIO", "Portfolio", {"drawdown": -0.05, "top1_weight": 0.10}))


def test_a_rule_fires_only_for_the_subject_that_breaches_it():
    findings = evaluate(context(day_change_pct=-0.08), [rule()])
    assert len(findings) == 1
    f = findings[0]
    assert f.subject_name == "Acme" and f.value == pytest.approx(-0.08)
    assert "fell below -5.00%" in f.message and "-8.00%" in f.message


def test_a_rule_scoped_to_one_holding_ignores_the_others():
    findings = evaluate(context(day_change_pct=-0.08), [rule(isin="B")])
    assert findings == []


def test_disabled_rules_never_fire():
    assert evaluate(context(day_change_pct=-0.50), [rule(enabled=False)]) == []


def test_missing_metrics_are_skipped_not_treated_as_zero():
    ctx = AlertContext(as_of=TODAY, securities={"A": Subject("A", "Acme", {})})
    assert evaluate(ctx, [rule()]) == []


def test_portfolio_rules_evaluate_against_the_portfolio_subject():
    r = rule(scope=PORTFOLIO, metric="drawdown", operator="lt", threshold=-0.03)
    findings = evaluate(context(day_change_pct=0.0), [r])
    assert len(findings) == 1 and findings[0].subject_key == "PORTFOLIO"
    assert "fall from the portfolio's peak" in findings[0].message


def test_moving_average_cross_reads_as_a_direction_not_a_threshold():
    up = evaluate(context(ma_cross=1), [rule(metric="ma_cross", operator="gte", threshold=1)])
    down = evaluate(context(ma_cross=-1), [rule(metric="ma_cross", operator="lte", threshold=-1)])
    assert "crossed above" in up[0].message and "crossed below" in down[0].message


def test_every_operator_behaves():
    checks = [("gt", 0.10, 0.11, True), ("gt", 0.10, 0.10, False), ("gte", 0.10, 0.10, True),
              ("lt", -0.05, -0.06, True), ("lte", -0.05, -0.05, True), ("lt", -0.05, -0.04, False)]
    for operator, threshold, value, should_fire in checks:
        r = rule(operator=operator, threshold=threshold)
        got = bool(evaluate(context(day_change_pct=value), [r]))
        assert got is should_fire, (operator, threshold, value)


def test_default_rules_install_once(session):
    first = seed_default_rules(session)
    assert first >= 12
    assert seed_default_rules(session) == 0
    labels = [r.label for r in load_rules(session)]
    assert len(labels) == len(set(labels))
    assert any("5% in a day" in label for label in labels)


def test_events_are_stored_and_deduplicated_by_cooldown(session):
    seed_default_rules(session)
    r = session.execute(select(AlertRule).where(AlertRule.label.like("%5% in a day%"))).scalars().first()
    r.cooldown_days = 5
    session.commit()
    findings = evaluate(context(day_change_pct=-0.08), [r])
    assert len(record(session, findings, TODAY)) == 1
    # same day and inside the cooldown: nothing new
    assert record(session, findings, TODAY) == []
    assert record(session, findings, TODAY + timedelta(days=3)) == []
    # after the cooldown it may fire again
    assert len(record(session, findings, TODAY + timedelta(days=6))) == 1
    assert session.query(AlertEvent).count() == 2


def test_cooldown_is_tracked_per_holding(session):
    seed_default_rules(session)
    r = session.execute(select(AlertRule).where(AlertRule.label.like("%5% in a day%"))).scalars().first()
    ctx = AlertContext(as_of=TODAY, securities={
        "A": Subject("A", "Acme", {"day_change_pct": -0.09}),
        "B": Subject("B", "Beta Co", {"day_change_pct": -0.09})})
    stored = record(session, evaluate(ctx, [r]), TODAY)
    assert {e.subject for e in stored} == {"Acme", "Beta Co"}


def test_inbox_shows_unacknowledged_events_until_they_are_cleared(session):
    seed_default_rules(session)
    r = load_rules(session)[0]
    record(session, evaluate(context(day_change_pct=-0.50, weight=0.9, rsi=95,
                                     from_high=-0.9, risk_share=0.9), load_rules(session)), TODAY)
    before = recent_events(session)
    assert before and all(not e.acknowledged for e in before)
    assert acknowledge_all(session) == len(before)
    assert recent_events(session) == []
    assert len(recent_events(session, include_acknowledged=True)) == len(before)
