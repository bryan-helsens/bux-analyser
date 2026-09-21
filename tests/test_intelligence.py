"""The judgement layer, end to end from an imported portfolio."""
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bux_analyser import intelligence
from bux_analyser.analytics.scoring import HOLD, REDUCE, Thresholds
from bux_analyser.db import AlertEvent, ScoreSnapshot, make_engine, session_factory
from bux_analyser.importers.persist import import_bux_file
from bux_analyser.marketdata.base import PriceSeries, Provenance
from bux_analyser.marketdata.store import MarketDataRouter, MarketDataStore
from bux_analyser.service import build_snapshot, refresh_market_data
from tests.test_app import FullPrices
from tests.test_service import Fx

FIX = Path(__file__).parent / "fixtures" / "bux_multi.csv"   # four holdings: enough to rank


class MultiPrices(FullPrices):
    levels = {"US0000000001": (130.0, "USD"), "NL0000000002": (18.0, "EUR"),
              "IE0000000003": (105.0, "EUR"), "US0000000004": (95.0, "USD")}
    profiles = {"US0000000001": ("stock", "Technology", "United States"),
                "NL0000000002": ("stock", "Industrials", "Netherlands"),
                "IE0000000003": ("etf", None, None),
                "US0000000004": ("stock", "Health Care", "United States")}


class CrashingPrices(MultiPrices):
    """A holding that has fallen hard, to make the risk rules fire."""
    def eod_history(self, ids, start, end):
        if ids.isin == "US0000000001":
            idx = pd.date_range(start, end, freq="D")
            path = np.concatenate([np.linspace(200.0, 200.0, len(idx) // 2),
                                   np.linspace(200.0, 70.0, len(idx) - len(idx) // 2)])
            return PriceSeries(pd.Series(path, index=idx), "USD",
                               Provenance("stub", "Stub", Provenance.now(), end))
        return super().eod_history(ids, start, end)


def _snapshot_with_store(tmp_path, provider=None, name="i.db"):
    session = session_factory(make_engine(tmp_path / name))()
    import_bux_file(session, FIX, archive=False)
    store = MarketDataStore(session, MarketDataRouter([provider or MultiPrices()], [Fx()]))
    refresh_market_data(session, store)
    return session, store, build_snapshot(session, store)


def _snapshot(tmp_path, provider=None, name="i.db"):
    session, _, snapshot = _snapshot_with_store(tmp_path, provider, name)
    return session, snapshot


def test_metric_frame_is_built_from_euro_prices(tmp_path):
    session, snap = _snapshot(tmp_path)
    frame = intelligence.build_metric_frame(snap)
    assert not frame.empty
    assert set(frame.columns) >= {"mom_12_1", "ret_6m", "vs_200d", "from_high",
                                  "volatility", "max_drawdown", "beta", "risk_share"}
    assert frame["volatility"].dropna().gt(0).all()


def test_intelligence_scores_and_recommends(tmp_path):
    session, snap = _snapshot(tmp_path)
    intel = intelligence.build(session, snap)
    assert intel.scores and intel.recommendations
    rec = intel.recommendations[0]
    assert rec.label in (HOLD, "WATCH", "REVIEW", "REDUCE")
    assert rec.reasons and rec.confidence in ("low", "medium", "high")
    assert any("Valuation is not assessed" in r for r in rec.risks)
    assert intel.recommendation_for(rec.isin) is rec


def test_fundamental_pillars_are_reported_missing_not_faked(tmp_path):
    """No statements are cached for these stubs, so the three fundamental pillars must
    stay unscored rather than defaulting to something that reads as a judgement."""
    session, snap = _snapshot(tmp_path)
    intel = intelligence.build(session, snap)
    score = next(iter(intel.scores.values()))
    assert score.pillars["valuation"].score is None
    assert score.coverage == pytest.approx(2 / 5)
    assert any("nothing can be judged expensive or cheap" in n for n in intel.notes)
    assert any("not with the wider market" in n for n in intel.notes)


def test_cached_statements_bring_the_fundamental_pillars_to_life(tmp_path):
    import json
    from datetime import datetime, timezone
    from bux_analyser.db import FundamentalsCache
    from bux_analyser.marketdata.edgar import parse_company_facts
    from bux_analyser.marketdata.fundamentals import to_json

    session, store, snap = _snapshot_with_store(tmp_path)
    facts = json.loads((Path(__file__).parent / "fixtures" / "edgar_companyfacts.json").read_text())
    for isin in ("US0000000001", "NL0000000002", "US0000000004"):
        f = parse_company_facts(facts, isin, "FIXT")
        session.add(FundamentalsCache(isin=isin, provider="edgar", payload=json.dumps(to_json(f)),
                                      currency="USD", quality="reported",
                                      latest_report=date(2026, 2, 10),
                                      retrieved_at=datetime.now(timezone.utc)))
    session.commit()

    intel = intelligence.build(session, snap, store=store)
    assert intel.fundamentals, "statements in the cache should produce metrics"
    scored = [s for s in intel.scores.values() if s.pillars["valuation"].available]
    assert len(scored) == 3
    for column in ("price_to_earnings", "return_on_equity", "ev_to_ebit", "fcf_yield"):
        assert column in intel.metrics.columns, column
    assert intel.metrics["return_on_equity"].notna().sum() == 3
    assert any("3 of 4 holdings have fundamentals" in n for n in intel.notes)


def test_score_snapshots_persist_and_explain_the_next_change(tmp_path):
    session, snap = _snapshot(tmp_path)
    intelligence.build(session, snap)
    stored = session.query(ScoreSnapshot).all()
    assert stored and all(s.pillars for s in stored)

    # backdate the stored snapshot and give it a different score, then rebuild
    for s in stored:
        s.as_of = snap.as_of.date() - timedelta(days=90)
        s.overall = 90.0
        s.pillars = '{"momentum": 95.0, "risk": 85.0}'
    session.commit()

    intel = intelligence.build(session, snap)
    change = next(iter(intel.changes.values()))
    assert change.previous_overall == 90.0 and change.delta < 0
    assert change.reasons()
    assert any("weakened" in r for r in change.reasons())


def test_a_large_position_is_flagged_for_reduction(tmp_path):
    session, snap = _snapshot(tmp_path)
    intel = intelligence.build(session, snap, thresholds=Thresholds(max_weight=0.01))
    assert intel.actionable
    assert all(r.label == REDUCE for r in intel.actionable)
    assert intel.recommendations[0].is_actionable      # actionable items sort first


def test_alert_context_carries_price_and_portfolio_metrics(tmp_path):
    session, snap = _snapshot(tmp_path)
    ctx = intelligence.build_alert_context(snap, date(2026, 9, 21))
    subject = next(iter(ctx.securities.values()))
    assert {"weight", "from_high", "rsi", "ma_cross"} <= set(subject.metrics)
    assert ctx.portfolio is not None and "top1_weight" in ctx.portfolio.metrics


def test_a_crash_fires_alerts_and_stores_them_once(tmp_path):
    session, snap = _snapshot(tmp_path, CrashingPrices())
    intel = intelligence.build(session, snap)
    assert intel.findings, "a 65% fall should breach a default rule"
    assert any("52-week high" in f.message or "month" in f.message for f in intel.findings)
    first = session.query(AlertEvent).count()
    assert first == len(intel.new_events) > 0
    # rebuilding the same day must not duplicate the inbox
    intelligence.build(session, snap)
    assert session.query(AlertEvent).count() == first


def test_a_portfolio_too_small_to_rank_says_so(tmp_path):
    single = Path(__file__).parent / "fixtures" / "bux_synthetic.csv"
    session = session_factory(make_engine(tmp_path / "one.db"))()
    import_bux_file(session, single, archive=False)
    store = MarketDataStore(session, MarketDataRouter([FullPrices()], [Fx()]))
    refresh_market_data(session, store)
    intel = intelligence.build(session, build_snapshot(session, store))
    score = next(iter(intel.scores.values()))
    assert score.overall is None
    assert "at least 3" in score.pillars["momentum"].unavailable_reason
    assert any("cannot produce a score" in n for n in intel.notes)


def test_intelligence_degrades_without_prices(tmp_path):
    session = session_factory(make_engine(tmp_path / "bare.db"))()
    import_bux_file(session, FIX, archive=False)
    store = MarketDataStore(session, MarketDataRouter([], []))
    snap = build_snapshot(session, store)
    intel = intelligence.build(session, snap)
    assert intel.scores == {} and intel.recommendations == []
    assert any("need price history" in n for n in intel.notes)
