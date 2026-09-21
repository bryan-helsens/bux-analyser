"""Assemble a briefing you can paste into a capable chat model.

A small model running locally is not good enough for this job, and sending the portfolio
anywhere automatically would be the wrong trade. So the application does what it is good
at, which is arithmetic over your data, and produces a document you hand over yourself.

Three things make the result trustworthy:

- **Every figure is computed here.** The model receives numbers, not a database, and is
  told in the document itself that it may not calculate or recall any of its own.
- **The gaps are written down.** A model shown a table with holes will fill them unless
  the holes are named. The limitations section names them.
- **You choose what leaves the machine.** In the default mode the briefing carries
  weights and percentages but no euro amounts, so it describes the shape of the
  portfolio without its size.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from ..analytics.portfolio import ETF_BUCKET
from .tools import PortfolioTools

RULES = """You are being given a factual briefing about one person's investment portfolio,
produced by their own software. Follow these rules:

1. Use only the figures in this briefing. Do not calculate, estimate, or recall any
   number of your own. If you need a figure that is not here, say it is not here.
2. The "What this briefing cannot tell you" section lists real gaps. Treat anything
   listed there as unknown, and never fill a gap with a plausible-sounding value.
3. Separate what is measured from what you infer. Label your inferences as yours.
4. Do not give price targets, forecasts, or instructions to buy or sell. Discussing what
   the data suggests, and what would change that reading, is useful; predicting is not.
5. Where the data does not support an answer, say so plainly and stop rather than
   reaching for something that sounds authoritative.
6. Be specific and brief. Name the holding and the figure you are reasoning from."""

QUESTIONS = {
    "General review": "Review this portfolio. What stands out, what concerns you, and what "
                      "would you want to look at more closely?",
    "Biggest risks": "What are the biggest risks in this portfolio, in order? Base each one "
                     "on a figure in the briefing.",
    "Concentration": "Am I too concentrated? Consider position sizes, sectors, countries and "
                     "currencies, and say which measure worries you most and why.",
    "Underperformers": "Which holdings are performing worst, and what does the data say about "
                       "why? Separate share-price weakness from currency effects.",
    "Quality of holdings": "Which holdings look strongest and weakest on the fundamental and "
                           "price evidence here? Say what evidence is missing for each.",
    "Diversification": "Would this portfolio be better diversified with a different mix? Use "
                       "the correlation and risk-contribution figures, and be clear about what "
                       "a historical comparison can and cannot show.",
    "What changed": "What has changed recently according to the scores, alerts and recent "
                    "returns, and which of those changes actually matter?",
    "Currency exposure": "How exposed am I to currency movements, and how much of my return "
                         "has come from currency rather than from the companies?",
    "Challenge me": "Argue against how this portfolio is built. What would a sceptical "
                    "professional say about it, using only the figures here?",
}


@dataclass
class BriefingOptions:
    include_amounts: bool = False        # euro amounts, or weights and percentages only
    include_holdings: bool = True
    include_performance: bool = True
    include_risk: bool = True
    include_exposure: bool = True
    include_scores: bool = True
    include_fundamentals: bool = True
    include_alerts: bool = True
    include_transactions: bool = False   # rarely useful and the longest section
    question: str = ""


@dataclass
class Briefing:
    markdown: str
    sections: list[str] = field(default_factory=list)
    redacted: bool = True
    approx_tokens: int = 0

    @property
    def characters(self) -> int:
        return len(self.markdown)


def _pct(v, digits=1, sign=True) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/a"
    return f"{float(v):{'+' if sign else ''}.{digits}%}"


def _num(v, digits=2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/a"
    return f"{float(v):,.{digits}f}"


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "_none_\n"
    head = "| " + " | ".join(columns) + " |"
    rule = "|" + "|".join("---" for _ in columns) + "|"
    body = ["| " + " | ".join(str(r.get(c, "")) for c in columns) + " |" for r in rows]
    return "\n".join([head, rule, *body]) + "\n"


def build(snapshot, intelligence=None, options: BriefingOptions | None = None,
          generated_at: datetime | None = None) -> Briefing:
    """Produce the briefing. Pure: reads a snapshot, writes text."""
    o = options or BriefingOptions()
    tools = PortfolioTools(snapshot, intelligence)
    led = snapshot.ledger
    money = o.include_amounts
    as_of = (generated_at or snapshot.as_of)
    out: list[str] = []
    sections: list[str] = []
    gaps: list[str] = []

    out.append(f"# Portfolio briefing — {as_of:%Y-%m-%d}")
    out.append("")
    out.append("## How to use this")
    out.append("")
    out.append(RULES)
    out.append("")

    # ---- 1. the portfolio itself -------------------------------------------------
    sections.append("Portfolio")
    invested = float(led.net_invested_base)
    total = snapshot.total_value
    total_pl = (total - invested) if total is not None else None
    twr = (float(snapshot.twr.iloc[-1]) - 1) if not snapshot.twr.empty else None
    out.append("## 1. The portfolio")
    out.append("")
    out.append(f"- Base currency: EUR. Broker: BUX. Holdings: {len(snapshot.holdings)}.")
    if not snapshot.history.empty:
        out.append(f"- History runs from {snapshot.history.index[0].date()} to "
                   f"{snapshot.history.index[-1].date()}.")
    if money:
        out.append(f"- Total value: €{_num(total)}, of which cash €{_num(snapshot.cash)}.")
        out.append(f"- Capital put in (deposits less withdrawals): €{_num(invested)}.")
        out.append(f"- Total profit: €{_num(total_pl)}"
                   + (f" ({_pct(total_pl / invested)})" if total_pl is not None and invested else ""))
        out.append(f"- Realised profit €{_num(float(led.realized_pl_base))}, dividends received "
                   f"€{_num(float(led.dividends_net_base))}, fees €{_num(-float(led.fees_base))}, "
                   f"taxes €{_num(-float(led.taxes_base))}.")
    else:
        cash_share = (snapshot.cash / total) if total else None
        out.append(f"- Amounts are withheld. The portfolio is treated as 100 units; cash is "
                   f"{_pct(cash_share, 1, sign=False)} of it.")
        out.append(f"- Total return on capital put in: "
                   f"{_pct(total_pl / invested) if total_pl is not None and invested else 'n/a'}.")
        out.append(f"- Dividends received, as a share of capital put in: "
                   f"{_pct(float(led.dividends_net_base) / invested) if invested else 'n/a'}. "
                   f"Fees and taxes: "
                   f"{_pct(-(float(led.fees_base) + float(led.taxes_base)) / invested) if invested else 'n/a'}.")
    out.append(f"- Time-weighted return since inception: {_pct(twr)}. This measures the "
               "investments, independent of deposit timing.")
    out.append(f"- Money-weighted return (XIRR, annualised): {_pct(snapshot.mwr)}.")
    out.append("")

    # ---- 2. holdings --------------------------------------------------------------
    if o.include_holdings:
        sections.append("Holdings")
        out.append("## 2. Holdings")
        out.append("")
        rows = []
        for h in sorted(snapshot.holdings, key=lambda x: -(x.weight or 0)):
            d = h.decomposition
            row = {
                "Holding": h.name, "Type": h.asset_type or "?",
                "Sector": (ETF_BUCKET if h.asset_type == "etf" else (h.sector or "?")),
                "Country": h.country or "?", "Traded in": h.currency,
                "Weight": _pct(h.weight, 1, sign=False),
                "Return": _pct(h.unrealized_pct),
                "From price": _pct(d.local) if d else "n/a",
                "From currency": _pct(d.fx) if d else "n/a",
            }
            if money:
                row["Value €"] = _num(h.value_base)
                row["Profit €"] = _num(h.unrealized_base)
            rows.append(row)
        columns = ["Holding", "Type", "Sector", "Country", "Traded in", "Weight", "Return",
                   "From price", "From currency"] + (["Value €", "Profit €"] if money else [])
        out.append(_table(rows, columns))
        out.append("Return is measured against average purchase cost. It splits exactly into a "
                   "share-price component and an exchange-rate component.")
        out.append("")

    # ---- 3. performance -----------------------------------------------------------
    if o.include_performance:
        sections.append("Performance")
        out.append("## 3. Against the market")
        out.append("")
        an = snapshot.analytics
        rows = []
        for b in (an.benchmarks if an else []):
            rows.append({"Benchmark": b.label, "Its return": _pct(b.total_return),
                         "My return": _pct(b.portfolio_total_return),
                         "Difference": _pct(b.excess),
                         "Basis": "total return" if b.total_return_basis else "price only"})
        out.append(_table(rows, ["Benchmark", "Its return", "My return", "Difference", "Basis"]))
        if not rows:
            gaps.append("No benchmark comparison: benchmark prices are not cached.")
        elif any(r["Basis"] == "price only" for r in rows):
            out.append("A price-only index excludes dividends, so it understates the benchmark "
                       "and flatters the comparison.")
        if an is not None and not an.monthly.empty:
            out.append("")
            out.append("Monthly returns (time-weighted, %):")
            out.append("")
            monthly = (an.monthly * 100).round(1)
            rows = [{"Year": str(year), **{c: ("" if pd.isna(v) else f"{v:+.1f}")
                                           for c, v in row.items()}}
                    for year, row in monthly.iterrows()]
            out.append(_table(rows, ["Year"] + [str(c) for c in monthly.columns]))
        out.append("")

    # ---- 4. risk -------------------------------------------------------------------
    if o.include_risk:
        sections.append("Risk")
        an = snapshot.analytics
        r = an.risk if an else None
        out.append("## 4. Risk")
        out.append("")
        if r is None or not r.n_obs:
            out.append("_No risk statistics: price history is not cached._")
            gaps.append("No risk statistics.")
        else:
            out.append(f"- Annualised volatility {_pct(r.volatility, 1, sign=False)}; "
                       f"Sharpe {_num(r.sharpe)}; Sortino {_num(r.sortino)} "
                       f"(risk-free rate assumed {r.risk_free:.0%}).")
            out.append(f"- Beta against {r.benchmark or 'the benchmark'}: {_num(r.beta)}. "
                       f"Tracking error {_pct(r.tracking_error, 1, sign=False)}.")
            out.append(f"- Up capture {_num(r.up_capture)}, down capture {_num(r.down_capture)}.")
            if r.drawdown:
                d = r.drawdown
                out.append(f"- Worst fall {_pct(d.max_drawdown)} from {d.peak} to {d.trough}, "
                           + (f"recovered {d.recovered}." if d.recovered else "not yet recovered."))
            out.append(f"- Daily value at risk (95%) {_pct(r.var_95, 2)}; conditional "
                       f"{_pct(r.cvar_95, 2)}.")
            out.append(f"- Based on {r.n_obs} trading days."
                       + ("" if r.reliable else " That is short; treat these as indicative."))
            if not r.reliable:
                gaps.append(f"Risk statistics rest on only {r.n_obs} days of history.")
        if an is not None and not an.risk_contribution.empty:
            out.append("")
            out.append("Share of portfolio volatility against share of value:")
            out.append("")
            rc = an.risk_contribution.head(10)
            out.append(_table([{"Holding": i, "Share of value": _pct(row["weight"], 1, sign=False),
                                "Share of risk": _pct(row["pct_of_risk"], 1, sign=False),
                                "Own volatility": _pct(row["volatility"], 1, sign=False)}
                               for i, row in rc.iterrows()],
                              ["Holding", "Share of value", "Share of risk", "Own volatility"]))
        if an is not None and not an.correlation.empty and len(an.correlation) > 1:
            pairs = []
            corr = an.correlation
            for i, a in enumerate(corr.index):
                for b in corr.columns[i + 1:]:
                    v = corr.loc[a, b]
                    if pd.notna(v):
                        pairs.append((abs(v), a, b, v))
            pairs.sort(reverse=True)
            if pairs:
                out.append("Most correlated pairs of daily euro returns: "
                           + "; ".join(f"{a} and {b} {v:.2f}" for _, a, b, v in pairs[:5]) + ".")
        if an is not None and an.concentration:
            c = an.concentration
            out.append(f"- Concentration: largest holding {c.top1:.1%}, top three {c.top3:.1%}, "
                       f"top five {c.top5:.1%}. {c.n} holdings behave like "
                       f"{c.effective_holdings:.1f} equally sized ones.")
        out.append("")

    # ---- 5. exposure ---------------------------------------------------------------
    if o.include_exposure:
        sections.append("Exposure")
        an = snapshot.analytics
        out.append("## 5. Exposure")
        out.append("")
        for key in ("sector", "country", "currency", "asset_type"):
            exposure = (an.exposures if an else {}).get(key)
            if exposure is None or exposure.weights.empty:
                continue
            listed = ", ".join(f"{k} {v:.1%}" for k, v in exposure.weights.head(8).items())
            out.append(f"- **{exposure.dimension}**: {listed}.")
            if exposure.unknown_weight > 0.01:
                out.append(f"  - {exposure.unknown_weight:.0%} could not be classified.")
        if an is not None and an.lookthrough_funds:
            out.append(f"- Funds broken into their holdings: {len(an.lookthrough_funds)}.")
        etf_weight = sum(h.weight or 0 for h in snapshot.holdings if h.asset_type == "etf")
        if etf_weight > 0 and not (an and an.lookthrough_funds):
            gaps.append(f"Funds are {etf_weight:.0%} of the portfolio and have not been broken "
                        "into their holdings, so sector, country and currency figures exclude "
                        "whatever those funds hold.")
        out.append("")

    # ---- 6. scores and signals -------------------------------------------------------
    if o.include_scores and intelligence is not None and intelligence.scores:
        sections.append("Scores")
        out.append("## 6. Scores and signals")
        out.append("")
        out.append("Scores rank this portfolio's holdings **against each other**, from 0 to 100. "
                   "They are not a market-wide ranking and have not been tested against what "
                   "happened next.")
        out.append("")
        rows = []
        for isin, sc in intelligence.scores.items():
            rec = intelligence.recommendation_for(isin)
            change = intelligence.changes.get(isin)
            pillars = sc.pillar_scores()
            rows.append({
                "Holding": sc.name,
                "Overall": _num(sc.overall, 0),
                "Change": _num(change.delta, 0) if change and change.delta is not None else "—",
                "Valuation": _num(pillars.get("valuation"), 0),
                "Growth": _num(pillars.get("growth"), 0),
                "Quality": _num(pillars.get("quality"), 0),
                "Momentum": _num(pillars.get("momentum"), 0),
                "Risk": _num(pillars.get("risk"), 0),
                "Signal": rec.label if rec else "—",
            })
        out.append(_table(rows, ["Holding", "Overall", "Change", "Valuation", "Growth", "Quality",
                                 "Momentum", "Risk", "Signal"]))
        actionable = intelligence.actionable
        if actionable:
            out.append("Flagged by the rules:")
            out.append("")
            for rec in actionable:
                out.append(f"- **{rec.name} — {rec.label}** ({rec.confidence} confidence): "
                           + " ".join(rec.reasons))
        out.append("")
        for note in intelligence.notes:
            gaps.append(note)

    # ---- 7. fundamentals ---------------------------------------------------------------
    if o.include_fundamentals and intelligence is not None:
        available = intelligence.fundamentals or {}
        sections.append("Fundamentals")
        out.append("## 7. Company fundamentals")
        out.append("")
        if not available:
            out.append("_No financial statements are cached for any holding._")
            gaps.append("No company fundamentals at all: nothing here can say whether a holding "
                        "is expensively or cheaply valued.")
        else:
            keys = [("price_to_earnings", "P/E"), ("ev_to_ebit", "EV/EBIT"),
                    ("price_to_sales", "P/S"), ("fcf_yield", "FCF yield"),
                    ("return_on_equity", "ROE"), ("gross_margin", "Gross margin"),
                    ("revenue_cagr_3y", "Revenue 3y"), ("debt_to_equity", "Debt/equity")]
            rows = []
            names = {h.isin: h.name for h in snapshot.holdings}
            for isin, m in available.items():
                row = {"Holding": names.get(isin, isin), "Period": str(m.period_end),
                       "Source": m.quality}
                for key, label in keys:
                    v = m.get(key)
                    row[label] = (_pct(v, 1, sign=False) if key in
                                  ("fcf_yield", "return_on_equity", "gross_margin", "revenue_cagr_3y")
                                  else _num(v))
                rows.append(row)
            out.append(_table(rows, ["Holding", "Period", "Source"] + [l for _, l in keys]))
            missing = [h.name for h in snapshot.holdings
                       if h.isin not in available and h.asset_type not in ("etf", "crypto")]
            if missing:
                gaps.append("No fundamentals for: " + ", ".join(missing)
                            + ". These holdings cannot be judged expensive or cheap.")
            if any(m.quality != "reported" for m in available.values()):
                gaps.append("Some fundamentals come from a scraped source with no filing date "
                            "and may be stale or wrong.")
        out.append("")

    # ---- 8. alerts -----------------------------------------------------------------------
    if o.include_alerts and intelligence is not None:
        sections.append("Alerts")
        out.append("## 8. Alerts that have fired")
        out.append("")
        findings = intelligence.findings
        if not findings:
            out.append("_Nothing has crossed a threshold._")
        else:
            for f in findings[:25]:
                out.append(f"- [{f.category}] {f.message}")
            if len(findings) > 25:
                out.append(f"- _and {len(findings) - 25} more_")
        out.append("")

    # ---- 9. what this cannot tell you ------------------------------------------------------
    out.append("## What this briefing cannot tell you")
    out.append("")
    out.append("Treat everything below as genuinely unknown. Do not fill these gaps with "
               "plausible values.")
    out.append("")
    standing = [
        "Nothing here forecasts anything. There are no price targets, earnings estimates or "
        "analyst opinions in this data.",
        "Scores rank these holdings against each other, not against the wider market, and have "
        "never been tested against subsequent returns.",
        "The signals are threshold rules the owner configured. They are a way of deciding what "
        "to look at, not evidence that acting on them helps.",
        "Prices are end-of-day or delayed, converted at European Central Bank reference rates, "
        "so they will differ slightly from a live broker screen.",
        "There is no news, no filings text and no information about anything that happened after "
        "the dates shown.",
    ]
    if not money:
        standing.append("Euro amounts have been withheld deliberately. Reason about proportions, "
                        "and do not try to infer the portfolio's size.")
    for g in standing + gaps:
        out.append(f"- {g}")
    out.append("")

    # ---- the question -----------------------------------------------------------------------
    if o.question:
        out.append("## My question")
        out.append("")
        out.append(o.question.strip())
        out.append("")

    markdown = "\n".join(out)
    return Briefing(markdown=markdown, sections=sections, redacted=not money,
                    approx_tokens=len(markdown) // 4)
