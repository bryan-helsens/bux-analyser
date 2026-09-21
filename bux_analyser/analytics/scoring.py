"""Explainable scores and rule-based recommendations.

Two deliberate limits, both surfaced to the reader rather than hidden:

1. **Only price-based pillars exist so far.** Valuation, growth and quality need
   company fundamentals, which are not loaded yet, so those pillars report as
   unavailable instead of being approximated from price.
2. **Ranks are relative to your own holdings**, not to a market universe. A momentum
   rank of 90 means "strongest among the things you own", which is a different and
   weaker statement than "in the top decile of European equities".

No language model is involved in producing a score or a label. Every number here is
arithmetic on stored data, and every recommendation names the rule that fired.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

HIGHER_BETTER = "higher"
LOWER_BETTER = "lower"
MIN_PEERS = 3            # below this a cross-sectional rank is meaningless
MIN_PILLARS = 2          # below this no overall score is produced
BOTTOM_THIRD = 100.0 / 3.0 + 1e-6


@dataclass
class Metric:
    key: str
    label: str
    value: float | None
    rank: float | None = None          # 0-100 among the portfolio's holdings
    direction: str = HIGHER_BETTER
    fmt: str = "pct"                   # pct | num
    note: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None

    def format(self) -> str:
        if self.value is None:
            return "n/a"
        return f"{self.value:+.1%}" if self.fmt == "pct" else f"{self.value:,.2f}"


@dataclass
class Pillar:
    key: str
    label: str
    metrics: list[Metric] = field(default_factory=list)
    score: float | None = None
    unavailable_reason: str = ""

    @property
    def available_metrics(self) -> list[Metric]:
        return [m for m in self.metrics if m.rank is not None]

    @property
    def coverage(self) -> float:
        return len(self.available_metrics) / len(self.metrics) if self.metrics else 0.0

    @property
    def available(self) -> bool:
        return self.score is not None


@dataclass
class Score:
    isin: str
    name: str
    as_of: date
    pillars: dict[str, Pillar] = field(default_factory=dict)
    overall: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return (sum(1 for p in self.pillars.values() if p.available) / len(self.pillars)
                if self.pillars else 0.0)

    def pillar_scores(self) -> dict[str, float]:
        return {k: p.score for k, p in self.pillars.items() if p.score is not None}


PILLAR_DEFINITIONS = {
    "momentum": ("Momentum", [
        ("mom_12_1", "12-month momentum, excluding the last month", HIGHER_BETTER, "pct"),
        ("ret_6m", "6-month return", HIGHER_BETTER, "pct"),
        ("vs_200d", "Price versus its 200-day average", HIGHER_BETTER, "pct"),
        ("from_high", "Distance below the 52-week high", HIGHER_BETTER, "pct"),
    ]),
    "risk": ("Risk", [
        ("volatility", "Annualised volatility", LOWER_BETTER, "pct"),
        ("max_drawdown", "Deepest fall from a peak", HIGHER_BETTER, "pct"),
        ("beta", "Sensitivity to the benchmark", LOWER_BETTER, "num"),
        ("risk_share", "Share of portfolio risk above its share of value", LOWER_BETTER, "pct"),
    ]),
    "valuation": ("Valuation", [
        ("price_to_earnings", "Price against earnings", LOWER_BETTER, "num"),
        ("ev_to_ebit", "Enterprise value against operating profit", LOWER_BETTER, "num"),
        ("price_to_sales", "Price against revenue", LOWER_BETTER, "num"),
        ("fcf_yield", "Free cash flow against price", HIGHER_BETTER, "pct"),
        ("price_to_book", "Price against book value", LOWER_BETTER, "num"),
    ]),
    "growth": ("Growth", [
        ("revenue_cagr_3y", "Revenue growth over three years", HIGHER_BETTER, "pct"),
        ("earnings_cagr_3y", "Earnings growth over three years", HIGHER_BETTER, "pct"),
        ("fcf_cagr_3y", "Free cash flow growth over three years", HIGHER_BETTER, "pct"),
        ("revenue_growth_1y", "Revenue growth over one year", HIGHER_BETTER, "pct"),
    ]),
    "quality": ("Quality", [
        ("return_on_equity", "Return on equity", HIGHER_BETTER, "pct"),
        ("return_on_capital", "Return on capital", HIGHER_BETTER, "pct"),
        ("gross_margin", "Gross margin", HIGHER_BETTER, "pct"),
        ("fcf_conversion", "Profit converted into cash", HIGHER_BETTER, "pct"),
        ("debt_to_equity", "Debt against equity", LOWER_BETTER, "num"),
    ]),
}
FUNDAMENTAL_PILLARS = ("valuation", "growth", "quality")
NEEDS_FUNDAMENTALS = ("Needs company fundamentals. None are cached for this holding: "
                      "free filings cover US-listed companies, and European names depend on a "
                      "best-effort source that may return nothing.")


def percentile_ranks(values: pd.Series, direction: str) -> pd.Series:
    """Rank values 0-100 across the portfolio's holdings.

    Ties share a rank. With fewer than three holdings carrying the metric the result is
    not informative, so nothing is returned.
    """
    v = values.dropna().astype(float)
    if len(v) < MIN_PEERS:
        return pd.Series(dtype=float)
    ascending = direction == HIGHER_BETTER
    pct = v.rank(ascending=ascending, method="average", pct=True)
    return pct * 100.0


def build_scores(metric_frame: pd.DataFrame, names: dict[str, str], as_of: date) -> dict[str, Score]:
    """Turn a table of raw metrics (rows = holdings, columns = metric keys) into scores."""
    scores: dict[str, Score] = {}
    if metric_frame is None or metric_frame.empty:
        return scores
    n_peers = len(metric_frame)
    too_few = (f"Ranking compares holdings with each other, which needs at least {MIN_PEERS}. "
               f"This portfolio has {n_peers}.") if n_peers < MIN_PEERS else ""

    ranks: dict[str, pd.Series] = {}
    for pillar_key, (_, definitions) in PILLAR_DEFINITIONS.items():
        for key, _, direction, _ in definitions:
            if key in metric_frame.columns:
                ranks[key] = percentile_ranks(metric_frame[key], direction)

    for isin in metric_frame.index:
        score = Score(isin=isin, name=names.get(isin, isin), as_of=as_of)
        for pillar_key, (label, definitions) in PILLAR_DEFINITIONS.items():
            pillar = Pillar(key=pillar_key, label=label)
            for key, metric_label, direction, fmt in definitions:
                raw = metric_frame[key].get(isin) if key in metric_frame.columns else None
                value = None if raw is None or pd.isna(raw) else float(raw)
                rank = ranks.get(key, pd.Series(dtype=float)).get(isin)
                pillar.metrics.append(Metric(key=key, label=metric_label, value=value,
                                             rank=None if rank is None or pd.isna(rank) else float(rank),
                                             direction=direction, fmt=fmt))
            usable = pillar.available_metrics
            if usable:
                pillar.score = float(np.mean([m.rank for m in usable]))
            elif too_few:
                pillar.unavailable_reason = too_few
            elif not any(m.available for m in pillar.metrics):
                pillar.unavailable_reason = (NEEDS_FUNDAMENTALS if pillar_key in FUNDAMENTAL_PILLARS
                                             else "None of these metrics could be computed for "
                                                  "this holding.")
            else:
                pillar.unavailable_reason = "Metrics exist but could not be ranked against the other holdings."
            score.pillars[pillar_key] = pillar

        available = [p.score for p in score.pillars.values() if p.score is not None]
        if len(available) >= MIN_PILLARS:
            score.overall = float(np.mean(available))
        elif too_few:
            score.notes.append(too_few)
        else:
            score.notes.append("Too few pillars available to combine into an overall score.")
        missing = [p.label for k, p in score.pillars.items() if not p.available]
        if missing:
            if all(not score.pillars[k].available for k in FUNDAMENTAL_PILLARS):
                score.notes.append("Not scored: " + ", ".join(missing) + ". " + NEEDS_FUNDAMENTALS)
            else:
                score.notes.append("Not scored: " + ", ".join(missing) + ".")
        scores[isin] = score
    return scores


# ---------------------------------------------------------------- change explanation
@dataclass
class ScoreChange:
    isin: str
    name: str
    previous_overall: float | None
    current_overall: float | None
    previous_date: date | None
    pillar_changes: dict[str, float] = field(default_factory=dict)

    @property
    def delta(self) -> float | None:
        if self.previous_overall is None or self.current_overall is None:
            return None
        return self.current_overall - self.previous_overall

    def reasons(self, limit: int = 3) -> list[str]:
        """The pillars that moved the overall score, largest first."""
        ordered = sorted(self.pillar_changes.items(), key=lambda kv: -abs(kv[1]))
        out = []
        for key, change in ordered[:limit]:
            if abs(change) < 0.5:
                continue
            label = PILLAR_DEFINITIONS.get(key, (key, []))[0]
            out.append(f"{label} {'improved' if change > 0 else 'weakened'} by {abs(change):.0f} points")
        return out


def compare_scores(current: Score, previous: dict | None) -> ScoreChange:
    """Diff a score against a stored snapshot. Purely mechanical subtraction."""
    prev_overall = previous.get("overall") if previous else None
    prev_pillars = (previous or {}).get("pillars") or {}
    changes = {}
    for key, pillar in current.pillars.items():
        before, now = prev_pillars.get(key), pillar.score
        if before is not None and now is not None:
            changes[key] = float(now) - float(before)
    return ScoreChange(isin=current.isin, name=current.name, previous_overall=prev_overall,
                       current_overall=current.overall,
                       previous_date=(previous or {}).get("as_of"), pillar_changes=changes)


# ---------------------------------------------------------------- recommendations
HOLD, WATCH, REVIEW, REDUCE, OPPORTUNITY = "HOLD", "WATCH", "REVIEW", "REDUCE", "OPPORTUNITY"


@dataclass
class Thresholds:
    max_weight: float = 0.15
    max_risk_share: float = 0.30
    score_drop: float = 12.0
    weak_pillar: float = 25.0
    cheap_pillar: float = 70.0
    drawdown: float = -0.35


@dataclass
class Recommendation:
    isin: str
    name: str
    label: str
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    invalidators: list[str] = field(default_factory=list)
    confidence: str = "low"
    as_of: date | None = None

    @property
    def is_actionable(self) -> bool:
        return self.label in (REVIEW, REDUCE)


def recommend(score: Score, change: ScoreChange | None, weight: float | None,
              risk_share: float | None, max_drawdown: float | None,
              thresholds: Thresholds | None = None) -> Recommendation:
    """Apply deterministic rules. The first rule that fires sets the label.

    This triages attention: it says where to look, not what to buy or sell. There is
    no evidence that acting on these labels improves returns, and the engine has not
    been backtested.
    """
    t = thresholds or Thresholds()
    rec = Recommendation(isin=score.isin, name=score.name, label=HOLD, as_of=score.as_of)

    if weight is not None and weight > t.max_weight:
        rec.label = REDUCE
        rec.reasons.append(f"Position is {weight:.1%} of the portfolio, above the {t.max_weight:.0%} limit you set")
        rec.invalidators.append(f"Falls back below {t.max_weight:.0%} of the portfolio")
    elif risk_share is not None and risk_share > t.max_risk_share:
        rec.label = REDUCE
        rec.reasons.append(f"Contributes {risk_share:.0%} of portfolio volatility, above the {t.max_risk_share:.0%} limit")
        rec.invalidators.append(f"Risk share falls below {t.max_risk_share:.0%}")
    else:
        drop = change.delta if change else None
        risk_pillar = score.pillars.get("risk")
        momentum_pillar = score.pillars.get("momentum")
        if drop is not None and drop <= -t.score_drop:
            rec.label = REVIEW
            rec.reasons.append(f"Score fell {abs(drop):.0f} points since {change.previous_date}")
            rec.reasons += change.reasons()
            rec.invalidators.append("Score recovers to its previous level")
        elif max_drawdown is not None and max_drawdown <= t.drawdown:
            rec.label = REVIEW
            rec.reasons.append(f"Has fallen {max_drawdown:.0%} from its peak")
            rec.invalidators.append("Price recovers towards the previous peak")
        elif risk_pillar and risk_pillar.score is not None and risk_pillar.score < t.weak_pillar:
            rec.label = REVIEW
            rec.reasons.append(f"Riskiest holdings in the portfolio on volatility, drawdown and beta "
                               f"(risk rank {risk_pillar.score:.0f} of 100)")
            rec.invalidators.append("Volatility or drawdown improves relative to the other holdings")
        elif (valuation_pillar := score.pillars.get("valuation")) and valuation_pillar.available \
                and valuation_pillar.score >= t.cheap_pillar \
                and (quality := score.pillars.get("quality")) and quality.available \
                and quality.score >= 50 \
                and momentum_pillar and momentum_pillar.score is not None \
                and momentum_pillar.score >= 30:
            rec.label = OPPORTUNITY
            rec.reasons.append(f"Among the cheaper of your holdings (valuation rank "
                               f"{valuation_pillar.score:.0f}) without weak quality or a falling price")
            rec.invalidators.append("Valuation rank falls below "
                                    f"{t.cheap_pillar:.0f}, or quality or price trend weakens")
        elif momentum_pillar and momentum_pillar.score is not None and momentum_pillar.score < t.weak_pillar:
            rec.label = WATCH
            rec.reasons.append(f"Weakest price trend in the portfolio (momentum rank "
                               f"{momentum_pillar.score:.0f} of 100)")
            rec.invalidators.append("Price trend recovers relative to the other holdings")
        else:
            rec.reasons.append("Nothing in the available data crosses a threshold you set")

    if weight is not None and weight > t.max_weight * 0.8 and rec.label != REDUCE:
        rec.risks.append(f"Position size {weight:.1%} is close to your {t.max_weight:.0%} limit")
    if max_drawdown is not None and max_drawdown < -0.2:
        rec.risks.append(f"Has been {max_drawdown:.0%} below its peak")
    valuation = score.pillars.get("valuation")
    if valuation is None or not valuation.available:
        rec.risks.append("Valuation is not assessed for this holding: no fundamentals are cached, "
                         "so an expensive holding cannot be told from a cheap one")
    elif valuation.score is not None and valuation.score <= BOTTOM_THIRD:
        rec.risks.append(f"In the bottom third of your holdings on valuation "
                         f"(rank {valuation.score:.0f} of 100), meaning relatively expensive")

    rec.confidence = _confidence(score, change)
    return rec


def _confidence(score: Score, change: ScoreChange | None) -> str:
    """Confidence reflects how much data stands behind the label, not how likely it
    is to be right. Nothing here has been validated against outcomes."""
    points = 0
    if score.coverage >= 0.8:
        points += 2
    elif score.coverage >= 0.4:
        points += 1
    if change is not None and change.previous_overall is not None:
        points += 1
    if all(p.coverage >= 0.75 for p in score.pillars.values() if p.available):
        points += 1
    return "high" if points >= 4 else ("medium" if points >= 2 else "low")


def unavailable_labels(scores: dict[str, Score] | None = None) -> dict[str, str]:
    """Labels the engine cannot produce, and why. Empty once the inputs exist."""
    if scores and any(s.pillars.get("valuation") and s.pillars["valuation"].available
                      for s in scores.values()):
        return {}
    return {OPPORTUNITY: "Needs valuation data to tell a cheap holding from an expensive one, "
                         "and no holding has fundamentals cached yet."}
