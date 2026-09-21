"""Scores, recommendations and alerts assembled from a snapshot.

Kept apart from `service` so the judgement layer can be read and tested on its own.
Everything here is arithmetic and rules over stored data: no model, no language model,
no external call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .alerts import AlertContext, Finding, Subject, evaluate, load_rules, record, seed_default_rules
from .analytics import indicators as ind
from .analytics.risk import annualised_volatility, beta_alpha, max_drawdown
from .analytics.scoring import (Recommendation, Score, ScoreChange, Thresholds, build_scores,
                                compare_scores, recommend)
from .db import AlertEvent, ScoreSnapshot

TRADING_DAYS = 252


@dataclass
class Intelligence:
    as_of: date
    metrics: pd.DataFrame = field(default_factory=pd.DataFrame)
    scores: dict[str, Score] = field(default_factory=dict)
    changes: dict[str, ScoreChange] = field(default_factory=dict)
    recommendations: list[Recommendation] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    new_events: list[AlertEvent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def recommendation_for(self, isin: str) -> Recommendation | None:
        return next((r for r in self.recommendations if r.isin == isin), None)

    @property
    def actionable(self) -> list[Recommendation]:
        return [r for r in self.recommendations if r.is_actionable]


def _risk_shares(snapshot) -> dict[str, float]:
    """Share of portfolio volatility per holding, keyed by ISIN.

    The risk table is indexed by display name for the dashboard, so it is mapped back.
    """
    an = snapshot.analytics
    if an is None or an.risk_contribution.empty:
        return {}
    by_name = {(h.name or h.isin): h.isin for h in snapshot.holdings}
    return {by_name[name]: float(row["pct_of_risk"])
            for name, row in an.risk_contribution.iterrows() if name in by_name}


def build_metric_frame(snapshot) -> pd.DataFrame:
    """Raw metrics per holding, in euros, ready to be ranked.

    Momentum and volatility are measured on the euro price, because that is the return
    the holder actually experienced. A dollar holding that rose while the dollar fell
    should not be scored as though the currency did not happen.
    """
    an = snapshot.analytics
    if an is None or an.eur_prices.empty:
        return pd.DataFrame()
    prices, returns = an.eur_prices, an.holding_returns
    benchmark = None
    if an.benchmarks:
        bm = an.benchmarks[0].index
        benchmark = bm.pct_change().dropna()

    rows = {}
    for h in snapshot.holdings:
        if h.isin not in prices.columns:
            continue
        p = prices[h.isin].dropna()
        if len(p) < 30:
            continue
        r = returns[h.isin].dropna() if h.isin in returns.columns else pd.Series(dtype=float)
        dd = max_drawdown(p)
        beta = None
        if benchmark is not None and len(r) > 30:
            beta, _ = beta_alpha(r, benchmark)
        rows[h.isin] = {
            "mom_12_1": ind.momentum(p),
            "ret_6m": ind.trailing_return(p, 126),
            "vs_200d": ind.above_average(p, 200),
            "from_high": ind.distance_from_high(p),
            "volatility": annualised_volatility(r),
            "max_drawdown": dd.max_drawdown if dd else None,
            "beta": beta,
            "risk_share": None,
        }
    frame = pd.DataFrame.from_dict(rows, orient="index")
    if frame.empty:
        return frame
    shares = _risk_shares(snapshot)
    weights = {h.isin: h.weight for h in snapshot.holdings if h.weight is not None}
    for isin in frame.index:
        share, weight = shares.get(isin), weights.get(isin)
        # what matters is carrying more risk than size warrants, so the excess is scored
        frame.loc[isin, "risk_share"] = (share - weight) if (share is not None and weight is not None) else np.nan
    return frame


def build_alert_context(snapshot, as_of: date) -> AlertContext:
    an = snapshot.analytics
    ctx = AlertContext(as_of=as_of)
    prices = an.eur_prices if an is not None else pd.DataFrame()
    shares = _risk_shares(snapshot)
    for h in snapshot.holdings:
        m: dict[str, float] = {}
        if h.day_change_pct is not None:
            m["day_change_pct"] = float(h.day_change_pct)
        if h.weight is not None:
            m["weight"] = float(h.weight)
        if h.isin in shares:
            m["risk_share"] = shares[h.isin]
        if not prices.empty and h.isin in prices.columns:
            p = prices[h.isin].dropna()
            if len(p) > 30:
                for key, days in (("week_change_pct", 5), ("month_change_pct", 21)):
                    v = ind.trailing_return(p, days)
                    if v is not None:
                        m[key] = v
                high = ind.distance_from_high(p)
                if high is not None:
                    m["from_high"] = high
                vs200 = ind.above_average(p, 200)
                if vs200 is not None:
                    m["vs_200d"] = vs200
                rsi_series = ind.rsi(p)
                if not rsi_series.empty:
                    m["rsi"] = float(rsi_series.iloc[-1])
                cross = ind.crossed(ind.sma(p, 50), ind.sma(p, 200))
                m["ma_cross"] = {"golden": 1.0, "death": -1.0}.get(cross, 0.0)
        ctx.securities[h.isin] = Subject(key=h.isin, name=h.name or h.isin, metrics=m)

    pm: dict[str, float] = {}
    if snapshot.day_change_pct is not None:
        pm["day_change_pct"] = float(snapshot.day_change_pct)
    if not snapshot.twr.empty:
        lv = snapshot.twr.dropna()
        if len(lv) > 1:
            pm["drawdown"] = float(lv.iloc[-1] / lv.cummax().iloc[-1] - 1)
    if an is not None and an.concentration is not None:
        pm["top1_weight"] = an.concentration.top1
        pm["top3_weight"] = an.concentration.top3
    ctx.portfolio = Subject(key="PORTFOLIO", name="Portfolio", metrics=pm)
    return ctx


def _previous_snapshot(session: Session, isin: str, as_of: date) -> dict | None:
    row = session.execute(
        select(ScoreSnapshot).where(ScoreSnapshot.isin == isin, ScoreSnapshot.as_of < as_of)
        .order_by(ScoreSnapshot.as_of.desc()).limit(1)).scalar()
    if row is None:
        return None
    return {"overall": float(row.overall) if row.overall is not None else None,
            "as_of": row.as_of, "pillars": json.loads(row.pillars or "{}")}


def _store_snapshot(session: Session, score: Score) -> None:
    existing = session.execute(
        select(ScoreSnapshot).where(ScoreSnapshot.isin == score.isin,
                                    ScoreSnapshot.as_of == score.as_of)).scalar()
    payload = json.dumps(score.pillar_scores())
    if existing is None:
        session.add(ScoreSnapshot(isin=score.isin, as_of=score.as_of,
                                  overall=score.overall, pillars=payload))
    else:
        existing.overall, existing.pillars = score.overall, payload


def build(session: Session, snapshot, thresholds: Thresholds | None = None,
          persist: bool = True) -> Intelligence:
    """Score every holding, explain any change, recommend an action, and fire alerts."""
    as_of = snapshot.as_of.date()
    intel = Intelligence(as_of=as_of)
    intel.metrics = build_metric_frame(snapshot)
    if intel.metrics.empty:
        intel.notes.append("Scores need price history. Refresh market data first.")
        return intel

    names = {h.isin: (h.name or h.isin) for h in snapshot.holdings}
    intel.scores = build_scores(intel.metrics, names, as_of)

    shares = _risk_shares(snapshot)
    for isin, score in intel.scores.items():
        previous = _previous_snapshot(session, isin, as_of) if persist else None
        change = compare_scores(score, previous)
        intel.changes[isin] = change
        holding = next((h for h in snapshot.holdings if h.isin == isin), None)
        intel.recommendations.append(recommend(
            score, change, weight=(holding.weight if holding else None),
            risk_share=shares.get(isin),
            max_drawdown=intel.metrics["max_drawdown"].get(isin),
            thresholds=thresholds))
        if persist:
            _store_snapshot(session, score)
    if persist:
        session.commit()

    intel.recommendations.sort(key=lambda r: (not r.is_actionable, r.name))

    if persist:
        seed_default_rules(session)
    rules = load_rules(session)
    context = build_alert_context(snapshot, as_of)
    intel.findings = evaluate(context, rules)
    if persist:
        intel.new_events = record(session, intel.findings, as_of)

    if intel.scores and not any(s.overall is not None for s in intel.scores.values()):
        intel.notes.append(
            "No holding could be scored. Ranking compares holdings with each other, so a "
            "portfolio of one or two priced holdings cannot produce a score.")
    covered = [s for s in intel.scores.values() if s.overall is not None]
    if covered and covered[0].coverage < 1.0:
        intel.notes.append(
            f"Scores use {covered[0].coverage:.0%} of the intended pillars. Valuation, growth and "
            "quality need company fundamentals, which are not loaded yet, so a holding cannot "
            "currently be judged expensive or cheap.")
    intel.notes.append("Ranks compare your holdings with each other, not with the wider market.")
    return intel
