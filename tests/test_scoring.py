from datetime import date

import numpy as np
import pandas as pd
import pytest

from bux_analyser.analytics.scoring import (HOLD, NEEDS_FUNDAMENTALS, OPPORTUNITY, REDUCE, REVIEW,
                                            WATCH, Score, ScoreChange, Thresholds, build_scores,
                                            compare_scores, percentile_ranks, recommend,
                                            unavailable_labels)

TODAY = date(2026, 9, 21)


def frame(**rows) -> pd.DataFrame:
    return pd.DataFrame.from_dict(rows, orient="index")


def sample_frame():
    return frame(
        STRONG={"mom_12_1": 0.40, "ret_6m": 0.20, "vs_200d": 0.15, "from_high": -0.02,
                "volatility": 0.18, "max_drawdown": -0.10, "beta": 0.9, "risk_share": 0.05},
        MIDDLE={"mom_12_1": 0.10, "ret_6m": 0.05, "vs_200d": 0.02, "from_high": -0.10,
                "volatility": 0.25, "max_drawdown": -0.20, "beta": 1.1, "risk_share": 0.10},
        WEAK={"mom_12_1": -0.30, "ret_6m": -0.15, "vs_200d": -0.20, "from_high": -0.45,
              "volatility": 0.50, "max_drawdown": -0.55, "beta": 1.8, "risk_share": 0.30},
    )


def test_percentile_ranks_follow_the_metric_direction():
    v = pd.Series({"a": 1.0, "b": 2.0, "c": 3.0})
    high = percentile_ranks(v, "higher")
    assert high["c"] > high["b"] > high["a"]
    low = percentile_ranks(v, "lower")
    assert low["a"] > low["b"] > low["c"]
    assert high["c"] == pytest.approx(100.0)


def test_percentile_ranks_refuse_to_rank_a_handful():
    assert percentile_ranks(pd.Series({"a": 1.0, "b": 2.0}), "higher").empty


def test_scores_rank_holdings_against_each_other():
    scores = build_scores(sample_frame(), {"STRONG": "Strong Co"}, TODAY)
    assert scores["STRONG"].name == "Strong Co"
    assert scores["STRONG"].overall > scores["MIDDLE"].overall > scores["WEAK"].overall
    assert scores["STRONG"].pillars["momentum"].score == pytest.approx(100.0)
    assert scores["WEAK"].pillars["risk"].score == pytest.approx(100 / 3, abs=1)


def test_fundamental_pillars_report_as_unavailable_not_as_zero():
    s = build_scores(sample_frame(), {}, TODAY)["STRONG"]
    for key in ("valuation", "growth", "quality"):
        p = s.pillars[key]
        assert p.score is None and not p.available
        assert p.unavailable_reason == NEEDS_FUNDAMENTALS
    assert s.coverage == pytest.approx(2 / 5)
    assert any("Valuation, Growth, Quality" in n for n in s.notes)


def test_overall_is_suppressed_when_too_little_is_available():
    f = frame(ONLY={"mom_12_1": 0.1}, OTHER={"mom_12_1": 0.2}, THIRD={"mom_12_1": 0.3})
    s = build_scores(f, {}, TODAY)["ONLY"]
    assert s.pillars["momentum"].available and not s.pillars["risk"].available
    assert s.overall is None
    assert any("Too few pillars" in n for n in s.notes)


def test_missing_metrics_are_skipped_rather_than_guessed():
    f = sample_frame()
    f.loc["MIDDLE", "beta"] = np.nan
    s = build_scores(f, {}, TODAY)["MIDDLE"]
    risk = s.pillars["risk"]
    assert risk.coverage == pytest.approx(0.75) and risk.score is not None
    assert next(m for m in risk.metrics if m.key == "beta").format() == "n/a"


def test_score_change_names_the_pillars_that_moved():
    current = build_scores(sample_frame(), {}, TODAY)["WEAK"]
    previous = {"overall": 80.0, "as_of": date(2026, 6, 1),
                "pillars": {"momentum": 90.0, "risk": 70.0}}
    ch = compare_scores(current, previous)
    assert ch.delta < 0
    reasons = ch.reasons()
    assert any("Momentum weakened" in r for r in reasons)
    assert compare_scores(current, None).delta is None


def test_reduce_fires_on_position_size_before_anything_else():
    s = build_scores(sample_frame(), {}, TODAY)["STRONG"]
    rec = recommend(s, None, weight=0.22, risk_share=0.05, max_drawdown=-0.05)
    assert rec.label == REDUCE and rec.is_actionable
    assert "22.0% of the portfolio" in rec.reasons[0]
    assert rec.invalidators and "15%" in rec.invalidators[0]


def test_reduce_fires_on_risk_share_even_when_the_position_is_small():
    s = build_scores(sample_frame(), {}, TODAY)["MIDDLE"]
    rec = recommend(s, None, weight=0.05, risk_share=0.45, max_drawdown=-0.05)
    assert rec.label == REDUCE and "45% of portfolio volatility" in rec.reasons[0]


def test_review_fires_on_a_falling_score():
    s = build_scores(sample_frame(), {}, TODAY)["MIDDLE"]
    change = ScoreChange(isin="MIDDLE", name="Mid", previous_overall=80.0,
                         current_overall=s.overall, previous_date=date(2026, 6, 1),
                         pillar_changes={"momentum": -25.0})
    rec = recommend(s, change, weight=0.05, risk_share=0.10, max_drawdown=-0.05)
    assert rec.label == REVIEW
    assert "fell" in rec.reasons[0] and any("Momentum weakened" in r for r in rec.reasons)


def test_review_fires_on_a_deep_drawdown():
    s = build_scores(sample_frame(), {}, TODAY)["MIDDLE"]
    rec = recommend(s, None, weight=0.05, risk_share=0.10, max_drawdown=-0.60)
    assert rec.label == REVIEW and "60%" in rec.reasons[0]


def test_watch_fires_on_the_weakest_trend():
    s = build_scores(sample_frame(), {}, TODAY)["WEAK"]
    rec = recommend(s, None, weight=0.05, risk_share=0.10, max_drawdown=-0.05,
                    thresholds=Thresholds(weak_pillar=40.0, drawdown=-0.9))
    assert rec.label in (WATCH, REVIEW)


def test_hold_is_the_default_and_says_so():
    s = build_scores(sample_frame(), {}, TODAY)["STRONG"]
    rec = recommend(s, None, weight=0.05, risk_share=0.05, max_drawdown=-0.05)
    assert rec.label == HOLD and not rec.is_actionable
    assert "crosses a threshold" in rec.reasons[0]


def test_every_recommendation_admits_valuation_is_missing():
    s = build_scores(sample_frame(), {}, TODAY)["STRONG"]
    rec = recommend(s, None, weight=0.05, risk_share=0.05, max_drawdown=-0.05)
    assert any("Valuation is not assessed" in r for r in rec.risks)
    assert OPPORTUNITY in unavailable_labels()


def test_confidence_rises_with_coverage_and_history():
    s = build_scores(sample_frame(), {}, TODAY)["STRONG"]
    bare = recommend(s, None, 0.05, 0.05, -0.05)
    change = ScoreChange("STRONG", "Strong", 70.0, s.overall, date(2026, 6, 1), {"momentum": 5.0})
    with_history = recommend(s, change, 0.05, 0.05, -0.05)
    assert bare.confidence in ("low", "medium")
    assert with_history.confidence in ("medium", "high")


def test_thresholds_are_configurable():
    s = build_scores(sample_frame(), {}, TODAY)["STRONG"]
    strict = recommend(s, None, weight=0.10, risk_share=0.05, max_drawdown=-0.05,
                       thresholds=Thresholds(max_weight=0.05))
    assert strict.label == REDUCE and "5%" in strict.reasons[0]
