"""The functions an assistant is allowed to call, and what they return.

Design rule: **the arithmetic is done here, in Python, and a model may only describe
the result.** Each function returns the numbers it used, a plain-language summary
written deterministically, and the sources behind it. That has two consequences:

- The app answers these questions correctly with no model present at all.
- When a model is attached, it has nothing to invent: the figures are in front of it
  and its only job is to put them into a sentence.

A model is never given the database, only this list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd


@dataclass
class ToolResult:
    name: str
    summary: str                                   # deterministic, safe to show as-is
    data: dict[str, Any] = field(default_factory=dict)
    table: pd.DataFrame | None = None
    sources: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def as_prompt_context(self) -> str:
        """A compact rendering for a model, carrying the caveats with the numbers."""
        lines = [f"[{self.name}] {self.summary}"]
        if self.data:
            lines.append("figures: " + ", ".join(f"{k}={_fmt(v)}" for k, v in self.data.items()
                                                 if v is not None))
        if self.table is not None and not self.table.empty:
            lines.append(self.table.head(15).to_string())
        for c in self.caveats:
            lines.append(f"caveat: {c}")
        if self.sources:
            lines.append("sources: " + "; ".join(self.sources))
        return "\n".join(lines)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.4g}"
    return str(v)


def _pct(v) -> str:
    return "unavailable" if v is None else f"{v:+.2%}"


def _eur(v) -> str:
    return "unavailable" if v is None else f"EUR {v:,.2f}"


class PortfolioTools:
    """A read-only view of one snapshot, exposed as named functions."""

    def __init__(self, snapshot, intelligence=None):
        self.snapshot = snapshot
        self.intel = intelligence

    # ---- lookup helpers ----------------------------------------------------
    def _find(self, query: str):
        if not query:
            return None
        q = query.strip().lower()
        for h in self.snapshot.holdings:
            if h.isin.lower() == q or (h.name or "").lower() == q:
                return h
        for h in self.snapshot.holdings:
            if q in (h.name or "").lower() or q in (h.ticker or "").lower():
                return h
        return None

    def _missing(self, name: str, query: str) -> ToolResult:
        known = ", ".join(sorted((h.name or h.isin) for h in self.snapshot.holdings))
        return ToolResult(name=name, summary=f"No holding matches '{query}'. Holdings are: {known}.")

    # ---- the callable surface ----------------------------------------------
    def get_portfolio_summary(self) -> ToolResult:
        s, led = self.snapshot, self.snapshot.ledger
        invested = float(led.net_invested_base)
        total_pl = (s.total_value - invested) if s.total_value is not None else None
        twr = (float(s.twr.iloc[-1]) - 1) if not s.twr.empty else None
        data = {"total_value_eur": s.total_value, "cash_eur": s.cash, "net_invested_eur": invested,
                "total_profit_eur": total_pl, "time_weighted_return": twr,
                "money_weighted_return": s.mwr, "holdings": len(s.holdings)}
        summary = (f"The portfolio is worth {_eur(s.total_value)} across {len(s.holdings)} holdings "
                   f"plus {_eur(s.cash)} cash. Against {_eur(invested)} put in, that is a profit of "
                   f"{_eur(total_pl)}. Time-weighted return since the first transaction is "
                   f"{_pct(twr)}.")
        caveats = list(s.warnings[:3])
        return ToolResult("get_portfolio_summary", summary, data, caveats=caveats,
                          sources=["BUX transaction export", "cached market prices", "ECB rates"])

    def get_position(self, query: str) -> ToolResult:
        h = self._find(query)
        if h is None:
            return self._missing("get_position", query)
        d = h.decomposition
        data = {"name": h.name, "isin": h.isin, "quantity": float(h.quantity),
                "average_cost_local": float(h.avg_cost_local), "currency": h.currency,
                "price": h.price, "value_eur": h.value_base, "profit_eur": h.unrealized_base,
                "return": h.unrealized_pct, "weight": h.weight,
                "dividends_eur": float(h.dividends_base),
                "costs_eur": float(h.fees_base + h.taxes_base)}
        parts = [f"{h.name}: {float(h.quantity):,.6g} shares at an average cost of "
                 f"{float(h.avg_cost_local):,.2f} {h.currency}, now worth {_eur(h.value_base)} "
                 f"({_pct(h.unrealized_pct)})."]
        if d and not d.is_single_currency:
            parts.append(f"Of that return, the share price contributed {_pct(d.local)} and the "
                         f"{h.currency} exchange rate {_pct(d.fx)}.")
        elif d:
            parts.append("It trades in euros, so none of the return came from currency.")
        if float(h.dividends_base):
            parts.append(f"It has paid {_eur(float(h.dividends_base))} in dividends.")
        return ToolResult("get_position", " ".join(parts), data, caveats=list(h.issues),
                          sources=["BUX transaction export", "cached market prices"])

    def get_performance(self) -> ToolResult:
        s, an = self.snapshot, self.snapshot.analytics
        rows = []
        if an is not None:
            for b in an.benchmarks:
                rows.append({"benchmark": b.label, "benchmark_return": b.total_return,
                             "my_return": b.portfolio_total_return, "difference": b.excess,
                             "basis": "total return" if b.total_return_basis else "price only"})
        table = pd.DataFrame(rows)
        if rows:
            best = max(rows, key=lambda r: (r["difference"] is not None, r["difference"] or 0))
            summary = (f"Against {best['benchmark']}, the portfolio returned "
                       f"{_pct(best['my_return'])} versus {_pct(best['benchmark_return'])}, a "
                       f"difference of {_pct(best['difference'])}.")
        else:
            summary = "No benchmark history is cached, so there is nothing to compare against yet."
        caveats = ["A price-only index excludes dividends and understates the benchmark."] \
            if any(r["basis"] == "price only" for r in rows) else []
        return ToolResult("get_performance", summary, {}, table, caveats=caveats,
                          sources=["cached benchmark prices"])

    def get_risk_metrics(self) -> ToolResult:
        an = self.snapshot.analytics
        r = an.risk if an else None
        if r is None or not r.n_obs:
            return ToolResult("get_risk_metrics",
                              "Risk statistics need price history, which is not cached yet.")
        data = {"volatility": r.volatility, "sharpe": r.sharpe, "sortino": r.sortino,
                "max_drawdown": r.max_drawdown, "beta": r.beta, "var_95_daily": r.var_95,
                "observations": r.n_obs}
        summary = (f"Annualised volatility is {_pct(r.volatility)} with a Sharpe ratio of "
                   f"{r.sharpe:.2f}. " if r.sharpe is not None else
                   f"Annualised volatility is {_pct(r.volatility)}. ")
        if r.drawdown:
            summary += (f"The worst fall was {r.drawdown.max_drawdown:.1%} from "
                        f"{r.drawdown.peak} to {r.drawdown.trough}.")
        caveats = [] if r.reliable else [f"Only {r.n_obs} days of history; treat as indicative."]
        return ToolResult("get_risk_metrics", summary, data, caveats=caveats,
                          sources=["cached market prices"])

    def get_allocation(self, dimension: str = "sector") -> ToolResult:
        an = self.snapshot.analytics
        exposure = (an.exposures if an else {}).get(dimension)
        if exposure is None:
            available = ", ".join((an.exposures if an else {}).keys()) or "none"
            return ToolResult("get_allocation",
                              f"No breakdown by '{dimension}'. Available: {available}.")
        top = exposure.weights.head(5)
        parts = ", ".join(f"{k} {v:.1%}" for k, v in top.items())
        summary = f"By {exposure.dimension.lower()}: {parts}."
        caveats = [exposure.note] if exposure.note else []
        if exposure.unknown_weight > 0.05:
            caveats.append(f"{exposure.unknown_weight:.0%} could not be classified.")
        return ToolResult("get_allocation", summary, {"unknown_weight": exposure.unknown_weight},
                          exposure.weights.rename("weight").to_frame(), caveats=caveats,
                          sources=["company profiles", "fund holdings files"])

    def get_risk_contributors(self, limit: int = 5) -> ToolResult:
        an = self.snapshot.analytics
        rc = an.risk_contribution if an else pd.DataFrame()
        if rc.empty:
            return ToolResult("get_risk_contributors",
                              "Needs at least two holdings with shared price history.")
        top = rc.head(limit)
        lead = top.index[0]
        summary = (f"{lead} carries the most risk: {top.iloc[0]['pct_of_risk']:.0%} of portfolio "
                   f"volatility from {top.iloc[0]['weight']:.0%} of the value.")
        return ToolResult("get_risk_contributors", summary, {},
                          top[["weight", "volatility", "pct_of_risk"]],
                          caveats=["Based on how holdings moved together in the past."],
                          sources=["cached market prices"])

    def get_score(self, query: str) -> ToolResult:
        h = self._find(query)
        if h is None:
            return self._missing("get_score", query)
        score = (self.intel.scores if self.intel else {}).get(h.isin)
        if score is None:
            return ToolResult("get_score", f"No score has been computed for {h.name}.")
        available = score.pillar_scores()
        unavailable = {k: p.unavailable_reason for k, p in score.pillars.items() if not p.available}
        parts = ", ".join(f"{k} {v:.0f}" for k, v in available.items())
        overall = f"{score.overall:.0f}" if score.overall is not None else "not produced"
        summary = f"{h.name} scores {overall} overall ({parts})."
        caveats = ["Ranks compare your own holdings, not the wider market."]
        if unavailable:
            caveats.append("Not scored: " + ", ".join(unavailable))
        return ToolResult("get_score", summary, {"overall": score.overall, **available},
                          caveats=caveats, sources=["cached prices", "cached filings"])

    def get_recommendation_reasons(self, query: str) -> ToolResult:
        h = self._find(query)
        if h is None:
            return self._missing("get_recommendation_reasons", query)
        rec = self.intel.recommendation_for(h.isin) if self.intel else None
        if rec is None:
            return ToolResult("get_recommendation_reasons", f"No signal was produced for {h.name}.")
        summary = f"{h.name} is marked {rec.label} ({rec.confidence} confidence). " + \
                  " ".join(rec.reasons)
        return ToolResult("get_recommendation_reasons", summary,
                          {"label": rec.label, "confidence": rec.confidence},
                          caveats=rec.risks + [f"Would stop applying if: {i}" for i in rec.invalidators]
                          + ["These rules have not been tested against what happened next."],
                          sources=["scores", "position sizes", "alert thresholds"])

    def get_alerts(self, limit: int = 10) -> ToolResult:
        findings = (self.intel.findings if self.intel else [])[:limit]
        if not findings:
            return ToolResult("get_alerts", "Nothing has crossed an alert threshold.")
        summary = f"{len(findings)} alert(s): " + "; ".join(f.message for f in findings[:3])
        table = pd.DataFrame([{"category": f.category, "subject": f.subject_name,
                               "message": f.message} for f in findings])
        return ToolResult("get_alerts", summary, {"count": len(findings)}, table,
                          sources=["alert rules you configured"])

    def get_fundamentals(self, query: str) -> ToolResult:
        h = self._find(query)
        if h is None:
            return self._missing("get_fundamentals", query)
        metrics = (self.intel.fundamentals if self.intel else {}).get(h.isin)
        if metrics is None:
            return ToolResult("get_fundamentals",
                              f"No financial statements are cached for {h.name}. Free filings "
                              "cover US-listed companies; European names depend on a "
                              "best-effort source.")
        keys = ("price_to_earnings", "ev_to_ebit", "fcf_yield", "return_on_equity",
                "gross_margin", "revenue_cagr_3y", "debt_to_equity")
        data = {k: metrics.get(k) for k in keys if metrics.get(k) is not None}
        summary = (f"{h.name}, period ending {metrics.period_end}: "
                   + ", ".join(f"{k.replace('_', ' ')} {_fmt(v)}" for k, v in data.items()))
        caveats = list(metrics.notes)
        if metrics.quality != "reported":
            caveats.append("Figures come from a scraped source with no filing date.")
        return ToolResult("get_fundamentals", summary, data, caveats=caveats,
                          sources=[f"{metrics.quality} statements, filed {metrics.reported_at}"])

    # ---- catalogue for a model ---------------------------------------------
    def catalogue(self) -> list[dict]:
        """Machine-readable descriptions, in the shape a tool-calling model expects."""
        return [
            {"name": "get_portfolio_summary", "description": "Total value, cash, profit and return.",
             "parameters": {}},
            {"name": "get_position", "description": "One holding's size, cost, value and return.",
             "parameters": {"query": "holding name or ISIN"}},
            {"name": "get_performance", "description": "Return against each benchmark.",
             "parameters": {}},
            {"name": "get_risk_metrics", "description": "Volatility, Sharpe, drawdown, beta, VaR.",
             "parameters": {}},
            {"name": "get_allocation", "description": "Breakdown by sector, country, currency or asset type.",
             "parameters": {"dimension": "sector | country | currency | asset_type"}},
            {"name": "get_risk_contributors", "description": "Which holdings carry portfolio risk.",
             "parameters": {"limit": "how many to return"}},
            {"name": "get_score", "description": "Pillar scores for one holding.",
             "parameters": {"query": "holding name or ISIN"}},
            {"name": "get_recommendation_reasons", "description": "Why a holding carries its signal.",
             "parameters": {"query": "holding name or ISIN"}},
            {"name": "get_alerts", "description": "Alerts that have fired.", "parameters": {"limit": "count"}},
            {"name": "get_fundamentals", "description": "Valuation, growth and quality figures.",
             "parameters": {"query": "holding name or ISIN"}},
        ]

    def call(self, name: str, **kwargs) -> ToolResult:
        """Dispatch by name, refusing anything not in the catalogue."""
        allowed = {t["name"] for t in self.catalogue()}
        if name not in allowed:
            return ToolResult(name, f"'{name}' is not an available function.")
        fn: Callable = getattr(self, name)
        try:
            return fn(**kwargs)
        except TypeError as e:
            return ToolResult(name, f"Wrong arguments for {name}: {e}")


SYSTEM_PROMPT = """You answer questions about one person's investment portfolio.

Rules you must follow:
- Use only figures returned by the functions. Never calculate, estimate or recall a number yourself.
- If a function says something is unavailable, say it is unavailable. Do not substitute a guess.
- Repeat the caveats that come with the figures you use.
- Separate what is measured from what is interpretation, and label interpretation as yours.
- Never give a price target, a forecast, or advice to buy or sell.
- If the data cannot answer the question, say so plainly and stop.
"""
