"""Alert rules and their evaluation.

Rules are rows in the database, so thresholds are the user's to change. Evaluation is
a pure function of a context object, which keeps it testable and keeps the engine from
reaching into providers or the network. Alerts describe what has already happened;
none of them is a prediction or a suggestion to trade.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import AlertEvent, AlertRule

SECURITY, PORTFOLIO = "security", "portfolio"
OPERATORS = {"gt": lambda v, t: v > t, "gte": lambda v, t: v >= t,
             "lt": lambda v, t: v < t, "lte": lambda v, t: v <= t}
OPERATOR_WORDS = {"gt": "rose above", "gte": "reached", "lt": "fell below", "lte": "fell to"}


@dataclass
class MetricSpec:
    label: str
    fmt: str = "pct"          # pct | num

    def format(self, value: float) -> str:
        return f"{value:+.2%}" if self.fmt == "pct" else f"{value:,.2f}"


SECURITY_METRICS = {
    "day_change_pct": MetricSpec("one-day price change"),
    "week_change_pct": MetricSpec("one-week price change"),
    "month_change_pct": MetricSpec("one-month price change"),
    "from_high": MetricSpec("distance below the 52-week high"),
    "weight": MetricSpec("share of the portfolio"),
    "risk_share": MetricSpec("share of portfolio risk"),
    "rsi": MetricSpec("relative strength index", "num"),
    "vs_200d": MetricSpec("price against its 200-day average"),
    "ma_cross": MetricSpec("50-day against 200-day average", "num"),
}
PORTFOLIO_METRICS = {
    "drawdown": MetricSpec("fall from the portfolio's peak"),
    "day_change_pct": MetricSpec("one-day portfolio change"),
    "top1_weight": MetricSpec("largest single holding"),
    "top3_weight": MetricSpec("largest three holdings combined"),
    "group_weight": MetricSpec("exposure to one group"),
}

DEFAULT_RULES = [
    ("Holding fell more than 5% in a day", SECURITY, "day_change_pct", "lt", -0.05, "price", 3),
    ("Holding fell more than 10% in a day", SECURITY, "day_change_pct", "lt", -0.10, "price", 1),
    ("Holding rose more than 10% in a day", SECURITY, "day_change_pct", "gt", 0.10, "price", 1),
    ("Holding fell more than 15% in a month", SECURITY, "month_change_pct", "lt", -0.15, "price", 14),
    ("Holding is more than 30% below its 52-week high", SECURITY, "from_high", "lt", -0.30, "price", 30),
    ("Position grew beyond 15% of the portfolio", SECURITY, "weight", "gt", 0.15, "portfolio", 30),
    ("Position carries more than 30% of portfolio risk", SECURITY, "risk_share", "gt", 0.30, "portfolio", 30),
    ("Overbought on RSI (above 70)", SECURITY, "rsi", "gt", 70, "technical", 21),
    ("Oversold on RSI (below 30)", SECURITY, "rsi", "lt", 30, "technical", 21),
    ("50-day average crossed above the 200-day", SECURITY, "ma_cross", "gte", 1, "technical", 30),
    ("50-day average crossed below the 200-day", SECURITY, "ma_cross", "lte", -1, "technical", 30),
    ("Portfolio is more than 15% below its peak", PORTFOLIO, "drawdown", "lt", -0.15, "portfolio", 14),
    ("Largest holding passed 20% of the portfolio", PORTFOLIO, "top1_weight", "gt", 0.20, "portfolio", 30),
    ("Top three holdings passed 50% of the portfolio", PORTFOLIO, "top3_weight", "gt", 0.50, "portfolio", 30),
]


@dataclass
class Subject:
    """One thing a rule can be evaluated against."""
    key: str                      # isin, or "PORTFOLIO"
    name: str
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class AlertContext:
    as_of: date
    securities: dict[str, Subject] = field(default_factory=dict)
    portfolio: Subject | None = None


@dataclass
class Finding:
    rule_id: int | None
    rule_label: str
    category: str
    subject_key: str
    subject_name: str
    metric: str
    value: float
    threshold: float
    operator: str
    message: str


def describe(rule_label: str, subject_name: str, metric: str, scope: str,
             operator: str, value: float, threshold: float) -> str:
    spec = (SECURITY_METRICS if scope == SECURITY else PORTFOLIO_METRICS).get(metric, MetricSpec(metric))
    if metric == "ma_cross":
        return (f"{subject_name}: the 50-day average crossed "
                f"{'above' if value > 0 else 'below'} the 200-day average")
    word = OPERATOR_WORDS.get(operator, "passed")
    return (f"{subject_name}: {spec.label} {word} {spec.format(threshold)} "
            f"(currently {spec.format(value)})")


def evaluate(context: AlertContext, rules: list[AlertRule]) -> list[Finding]:
    """Check every enabled rule against the context. Pure: no database writes."""
    findings: list[Finding] = []
    for rule in rules:
        if not rule.enabled:
            continue
        test = OPERATORS.get(rule.operator)
        if test is None:
            continue
        threshold = float(rule.threshold)
        subjects = []
        if rule.scope == PORTFOLIO:
            if context.portfolio is not None:
                subjects = [context.portfolio]
        elif rule.isin:
            s = context.securities.get(rule.isin)
            subjects = [s] if s else []
        else:
            subjects = list(context.securities.values())
        for subject in subjects:
            value = subject.metrics.get(rule.metric)
            if value is None or pd.isna(value):
                continue
            if not test(float(value), threshold):
                continue
            findings.append(Finding(
                rule_id=getattr(rule, "id", None), rule_label=rule.label, category=rule.category,
                subject_key=subject.key, subject_name=subject.name, metric=rule.metric,
                value=float(value), threshold=threshold, operator=rule.operator,
                message=describe(rule.label, subject.name, rule.metric, rule.scope,
                                 rule.operator, float(value), threshold)))
    return findings


# ---------------------------------------------------------------- persistence
def seed_default_rules(session: Session) -> int:
    """Install the starting rule set once. Existing rules are never overwritten."""
    existing = {r.label for r in session.execute(select(AlertRule)).scalars()}
    added = 0
    for label, scope, metric, operator, threshold, category, cooldown in DEFAULT_RULES:
        if label in existing:
            continue
        session.add(AlertRule(label=label, scope=scope, metric=metric, operator=operator,
                              threshold=threshold, category=category, cooldown_days=cooldown,
                              enabled=True))
        added += 1
    if added:
        session.commit()
    return added


def load_rules(session: Session) -> list[AlertRule]:
    return list(session.execute(select(AlertRule).order_by(AlertRule.category, AlertRule.label)).scalars())


def record(session: Session, findings: list[Finding], as_of: date) -> list[AlertEvent]:
    """Store findings that are not already covered by a recent event.

    The cooldown stops a rule that stays true from filling the inbox with the same
    line every day.
    """
    stored: list[AlertEvent] = []
    rules = {r.id: r for r in load_rules(session)}
    for f in findings:
        rule = rules.get(f.rule_id)
        cooldown = timedelta(days=rule.cooldown_days if rule else 7)
        recent = session.execute(
            select(AlertEvent).where(AlertEvent.rule_id == f.rule_id,
                                     AlertEvent.isin == (None if f.subject_key == "PORTFOLIO" else f.subject_key))
            .order_by(AlertEvent.fired_at.desc()).limit(1)).scalar()
        if recent is not None and (as_of - recent.as_of) < cooldown:
            continue
        event = AlertEvent(rule_id=f.rule_id, fired_at=datetime.now(timezone.utc), as_of=as_of,
                           isin=None if f.subject_key == "PORTFOLIO" else f.subject_key,
                           subject=f.subject_name, value=f.value, message=f.message,
                           category=f.category)
        session.add(event)
        stored.append(event)
    if stored:
        session.commit()
    return stored


def recent_events(session: Session, limit: int = 100, include_acknowledged: bool = False) -> list[AlertEvent]:
    q = select(AlertEvent).order_by(AlertEvent.fired_at.desc()).limit(limit)
    if not include_acknowledged:
        q = select(AlertEvent).where(AlertEvent.acknowledged.is_(False)) \
            .order_by(AlertEvent.fired_at.desc()).limit(limit)
    return list(session.execute(q).scalars())


def acknowledge_all(session: Session) -> int:
    events = session.execute(select(AlertEvent).where(AlertEvent.acknowledged.is_(False))).scalars()
    n = 0
    for e in events:
        e.acknowledged = True
        n += 1
    if n:
        session.commit()
    return n
