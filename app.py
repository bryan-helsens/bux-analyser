"""BUX Analyser dashboard. Run: streamlit run app.py

Everything stays on this machine. Only public symbols are sent to market-data
sources; transactions, quantities and amounts never leave the local database.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import numpy as np

from bux_analyser import alerts as alert_engine
from bux_analyser import intelligence as intel_engine
from bux_analyser.analytics import simulate as sim
from bux_analyser.analytics.portfolio import ETF_BUCKET
from bux_analyser.ai.tools import PortfolioTools
from bux_analyser.analytics import backtest as bt
from bux_analyser.analytics import factors as fa
from bux_analyser.analytics.scoring import PILLAR_DEFINITIONS, Thresholds, unavailable_labels
from bux_analyser.db import make_engine, session_factory
from bux_analyser.importers.persist import import_bux_file
from bux_analyser.service import build_snapshot, default_store, refresh_market_data
from bux_analyser.ui import charts
from bux_analyser.ui.theme import detect_theme

st.set_page_config(page_title="BUX Analyser", layout="wide", page_icon="📈")


@st.cache_resource
def _session():
    return session_factory(make_engine())()


session = _session()
store = default_store(session)
theme = detect_theme()


# ---------------------------------------------------------------- formatting helpers
def eur(x, digits=2, dash="n/a"):
    return dash if x is None or (isinstance(x, float) and pd.isna(x)) else f"€ {float(x):,.{digits}f}"


def pct(x, digits=2, dash="n/a", sign=True):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return dash
    return f"{float(x):{'+' if sign else ''}.{digits}%}"


def num(x, digits=2, dash="n/a"):
    return dash if x is None or (isinstance(x, float) and pd.isna(x)) else f"{float(x):,.{digits}f}"


def provenance_line(p) -> str:
    if p is None:
        return "unavailable"
    d = p.data_date.isoformat() if p.data_date else "unknown date"
    extra = f" · {p.note}" if p.note else ""
    return f"{p.source} · data {d} · fetched {p.retrieved_at:%Y-%m-%d %H:%M} UTC · {p.kind}{extra}"


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.subheader("Data")
    up = st.file_uploader("BUX transaction-history CSV", type=["csv"])
    if up is not None and st.button("Import", width="stretch"):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / up.name
            p.write_bytes(up.getvalue())
            r = import_bux_file(session, p)
        st.success(f"{r.rows} rows, {r.new_rows} new, {r.new_txns} transactions"
                   + (" (already imported)" if r.already_imported else ""))
        st.write("Cash check:", "matches BUX balances" if r.cash_ok else "MISMATCH")
        for m in r.cash_messages:
            st.error(m)
        for w in r.warnings:
            st.warning(w)

    if st.button("Refresh market data", width="stretch", type="primary"):
        with st.spinner("Fetching prices, rates and company profiles…"):
            msgs = refresh_market_data(session, store)
        st.session_state["refresh_msgs"] = msgs
        st.rerun()

    for m in st.session_state.pop("refresh_msgs", []):
        st.warning(m)

    st.caption("Prices and profiles: Yahoo Finance. Exchange rates: ECB via frankfurter.dev. "
               "Your transactions stay in the local database.")

    with st.expander("Provider health"):
        for h in store.router.health():
            st.write(f"**{h.name}** · {h.calls} calls · {h.failures} failures")
            if h.last_error:
                st.caption(h.last_error[:200])

snapshot = build_snapshot(session, store, refresh=False)
led = snapshot.ledger
an = snapshot.analytics

if not led.positions and led.cash_base == 0:
    st.title("BUX Analyser")
    st.info("No data yet. Import a BUX transaction-history CSV from the sidebar to begin.")
    st.stop()

# ---------------------------------------------------------------- header
invested = float(led.net_invested_base)
total_pl = (snapshot.total_value - invested) if snapshot.total_value is not None else None
twr_total = (snapshot.twr.iloc[-1] - 1) if not snapshot.twr.empty else None
primary_bm = an.benchmarks[0] if (an and an.benchmarks) else None

st.title("Portfolio")
k = st.columns(6)
k[0].metric("Total value", eur(snapshot.total_value),
            pct(snapshot.day_change_pct) if snapshot.day_change_pct is not None else None,
            help="Holdings at their latest price converted at the ECB rate, plus cash.")
k[1].metric("Total P/L", eur(total_pl),
            pct(total_pl / invested) if (total_pl is not None and invested) else None,
            help="Value minus the capital you put in. Includes dividends, fees and taxes.")
k[2].metric("Time-weighted return", pct(twr_total),
            help="How the investments performed, independent of when you deposited.")
k[3].metric("Money-weighted return", pct(snapshot.mwr),
            help="Annualised XIRR on your actual deposits and withdrawals.")
k[4].metric("vs " + (primary_bm.label if primary_bm else "benchmark"),
            pct(primary_bm.excess) if primary_bm else "n/a",
            help="Total return over the same window, minus the benchmark's.")
k[5].metric("Cash", eur(snapshot.cash), help="Reconciled against the BUX running balance at import.")

for w in snapshot.warnings:
    st.warning(w)

tabs = st.tabs(["Overview", "Performance", "Exposure", "Risk", "Signals", "Scenarios",
                "Holdings", "Research", "Transactions"])

# ---------------------------------------------------------------- holdings frame
rows = []
for h in snapshot.holdings:
    d = h.decomposition
    rows.append({
        "Name": h.name, "ISIN": h.isin, "Symbol": h.ticker or "—", "Type": h.asset_type or "—",
        "Sector": (ETF_BUCKET if h.asset_type == "etf" else (h.sector or "—")),
        "Qty": float(h.quantity), "Avg cost": float(h.avg_cost_local), "Ccy": h.currency,
        "Price": h.price, "Value": h.value_base, "P/L": h.unrealized_base, "P/L %": h.unrealized_pct,
        "Today %": h.day_change_pct, "Weight": h.weight,
        "Price return": (d.local if d else None), "Currency return": (d.fx if d else None),
        "Dividends": float(h.dividends_base), "Costs": float(h.fees_base + h.taxes_base),
        "Issues": "; ".join(h.issues),
    })
holdings_df = pd.DataFrame(rows).sort_values("Value", ascending=False, na_position="last")
value_by_name = holdings_df.dropna(subset=["Value"]).set_index("Name")["Value"]

NUM_COLS = {
    "Value": st.column_config.NumberColumn("Value €", format="€ %.2f"),
    "P/L": st.column_config.NumberColumn("P/L €", format="€ %.2f"),
    "P/L %": st.column_config.NumberColumn(format="percent"),
    "Today %": st.column_config.NumberColumn(format="percent"),
    "Weight": st.column_config.NumberColumn(format="percent"),
    "Price return": st.column_config.NumberColumn(format="percent"),
    "Currency return": st.column_config.NumberColumn(format="percent"),
    "Qty": st.column_config.NumberColumn(format="%.6f"),
    "Avg cost": st.column_config.NumberColumn(format="%.4f"),
    "Price": st.column_config.NumberColumn(format="%.2f"),
    "Dividends": st.column_config.NumberColumn("Dividends €", format="€ %.2f"),
    "Costs": st.column_config.NumberColumn("Costs €", format="€ %.2f"),
}

# ---------------------------------------------------------------- Overview
with tabs[0]:
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Portfolio value")
        st.plotly_chart(charts.value_over_time(snapshot.history, theme), width="stretch", key="value_hist")
    with right:
        st.subheader("Where the money is")
        alloc = value_by_name.copy()
        if snapshot.cash > 0:
            alloc["Cash"] = snapshot.cash
        st.plotly_chart(charts.horizontal_bars(alloc, theme, money=True, height=380), width="stretch", key="alloc")

    st.subheader("Holdings")
    st.dataframe(holdings_df.drop(columns=["Price return", "Currency return", "Issues"]),
                 width="stretch", hide_index=True, column_config=NUM_COLS)

    c = st.columns(5)
    c[0].metric("Realised P/L", eur(led.realized_pl_base))
    c[1].metric("Dividends received", eur(led.dividends_net_base))
    c[2].metric("Interest and other income", eur(led.interest_base + led.income_base))
    c[3].metric("Fees paid", eur(-led.fees_base))
    c[4].metric("Taxes paid", eur(-led.taxes_base),
                help="Transaction taxes plus dividend withholding tax.")

# ---------------------------------------------------------------- Performance
with tabs[1]:
    st.subheader("Against the market")
    st.plotly_chart(charts.benchmark_comparison(snapshot.twr, an.benchmarks if an else [], theme),
                    width="stretch")
    if an and an.benchmarks:
        bm_rows = []
        for b in an.benchmarks:
            bm_rows.append({"Benchmark": b.label, "Benchmark return": b.total_return,
                            "My return": b.portfolio_total_return, "Difference": b.excess,
                            "Basis": "total return" if b.total_return_basis else "price only",
                            "Note": b.note})
        st.dataframe(pd.DataFrame(bm_rows), width="stretch", hide_index=True,
                     column_config={"Benchmark return": st.column_config.NumberColumn(format="percent"),
                                    "My return": st.column_config.NumberColumn(format="percent"),
                                    "Difference": st.column_config.NumberColumn(format="percent")})
        if any(not b.total_return_basis for b in an.benchmarks):
            st.caption("A price-only index excludes dividends, so it understates the true benchmark "
                       "return and flatters the comparison.")
    else:
        st.info("Benchmarks appear once market data has been refreshed.")

    st.subheader("Monthly returns")
    st.plotly_chart(charts.returns_heatmap(an.monthly if an else pd.DataFrame(), theme), width="stretch", key="monthly")
    st.caption("Time-weighted, so deposits and withdrawals do not distort a month.")

    st.subheader("Stock price versus exchange rate")
    dec = holdings_df.dropna(subset=["Price return"]).set_index("Name")[["Price return", "Currency return"]]
    dec.columns = ["local", "fx"]
    non_eur = dec[holdings_df.set_index("Name").loc[dec.index, "Ccy"] != "EUR"]
    st.plotly_chart(charts.decomposition_bars(non_eur if not non_eur.empty else dec, theme), width="stretch", key="decomp")
    if an and an.decomposition:
        d = an.decomposition
        st.caption(f"Across the portfolio, the share prices contributed {pct(d.local)} and the "
                   f"exchange rate {pct(d.fx)}, for a combined {pct(d.total)} on cost. "
                   "Euro-denominated holdings have no currency effect by definition.")

    if an is not None and not an.contributions.empty:
        st.subheader("Who moved the needle")
        names = holdings_df.set_index("ISIN")["Name"].to_dict()
        contrib = an.contributions.rename(index=lambda i: names.get(i, i))
        st.plotly_chart(charts.horizontal_bars(contrib, theme, height=None), width="stretch", key="contrib")
        st.caption("Each holding's weight multiplied by its return over the period held.")

# ---------------------------------------------------------------- Exposure
with tabs[2]:
    if not an or not an.exposures:
        st.info("Exposure needs prices. Refresh market data first.")
    else:
        conc = an.concentration
        if conc:
            c = st.columns(4)
            c[0].metric("Largest holding", pct(conc.top1, 1, sign=False))
            c[1].metric("Top 3", pct(conc.top3, 1, sign=False))
            c[2].metric("Top 5", pct(conc.top5, 1, sign=False))
            c[3].metric("Effective holdings", num(conc.effective_holdings, 1),
                        help=f"{conc.n} holdings, but concentration makes them behave like "
                             f"{conc.effective_holdings:.1f} equally sized ones.")
        grid = st.columns(2)
        for i, key in enumerate(["sector", "currency", "country", "asset_type"]):
            ex = an.exposures.get(key)
            if ex is None:
                continue
            with grid[i % 2]:
                st.subheader(ex.dimension)
                st.plotly_chart(charts.horizontal_bars(ex.weights, theme), width="stretch", key=f"exposure_{key}")
                if ex.note:
                    st.caption(ex.note)
                if ex.unknown_weight > 0.01:
                    st.caption(f"{ex.unknown_weight:.0%} of the portfolio has no {ex.dimension.lower()} "
                               "data from a free source.")

# ---------------------------------------------------------------- Risk
with tabs[3]:
    r = an.risk if an else None
    if r is None or not r.n_obs:
        st.info("Risk statistics need price history. Refresh market data first.")
    else:
        if not r.reliable:
            st.warning(f"Only {r.n_obs} days of history are available. These figures are indicative.")
        c = st.columns(5)
        c[0].metric("Volatility", pct(r.volatility, 1, sign=False), help="Annualised standard deviation.")
        c[1].metric("Sharpe", num(r.sharpe), help=f"Return per unit of risk, above {r.risk_free:.0%}.")
        c[2].metric("Sortino", num(r.sortino), help="Like Sharpe, but counting only downside moves.")
        c[3].metric("Max drawdown", pct(r.max_drawdown, 1))
        c[4].metric("Beta vs " + (r.benchmark or "—"), num(r.beta))

        c = st.columns(5)
        c[0].metric("Daily VaR 95%", pct(r.var_95, 2), help="On 19 days out of 20, the loss was smaller.")
        c[1].metric("Daily CVaR 95%", pct(r.cvar_95, 2), help="Average loss on the worst 5% of days.")
        c[2].metric("Tracking error", pct(r.tracking_error, 1, sign=False))
        c[3].metric("Up capture", num(r.up_capture))
        c[4].metric("Down capture", num(r.down_capture))

        if r.drawdown:
            d = r.drawdown
            recovered = f"recovered {d.recovered}" if d.recovered else "not yet recovered"
            st.caption(f"Worst fall {d.max_drawdown:.1%}, from the peak on {d.peak} to the low on "
                       f"{d.trough}, {recovered} ({d.days_under_water} days below the peak).")
        st.plotly_chart(charts.drawdown_area(snapshot.twr, theme), width="stretch", key="drawdown")

        st.subheader("Which holdings carry the risk")
        rc = an.risk_contribution
        if rc.empty:
            st.info("Needs at least two holdings with enough shared price history.")
        else:
            st.plotly_chart(charts.grouped_bars(rc[["weight", "pct_of_risk"]].rename(
                columns={"weight": "Share of value", "pct_of_risk": "Share of risk"}), theme),
                width="stretch")
            st.caption("A holding whose share of risk exceeds its share of value is pulling the "
                       "portfolio around more than its size suggests.")
            st.dataframe(rc.rename(columns={"weight": "Weight", "volatility": "Volatility",
                                            "marginal": "Marginal risk", "contribution": "Risk contribution",
                                            "pct_of_risk": "Share of risk"}),
                         width="stretch",
                         column_config={c: st.column_config.NumberColumn(format="percent")
                                        for c in ["Weight", "Volatility", "Share of risk"]})

        st.subheader("How the holdings move together")
        st.plotly_chart(charts.correlation_heatmap(an.correlation, theme), width="stretch", key="corr")
        st.caption("Daily euro returns. Two holdings close to 1.0 diversify each other very little.")


# ---------------------------------------------------------------- Signals
with st.sidebar:
    st.subheader("Thresholds")
    max_weight = st.slider("Flag a position above", 0.05, 0.40, 0.15, 0.01, format="%.0f%%",
                           help="Positions larger than this are flagged for review.")
    max_risk = st.slider("Flag a risk share above", 0.10, 0.60, 0.30, 0.01, format="%.0f%%",
                         help="A holding contributing more than this share of portfolio volatility.")
thresholds = Thresholds(max_weight=max_weight, max_risk_share=max_risk)

intel = intel_engine.build(session, snapshot, thresholds=thresholds, store=store)

with tabs[4]:
    st.caption("Scores rank your holdings against each other from data you already hold. "
               "They are a way to decide what to look at first, not advice, and they have "
               "not been tested against what actually happened next.")
    for n in intel.notes:
        st.info(n)

    if not intel.scores:
        st.stop() if False else None
    else:
        score_rows = []
        for isin, sc in intel.scores.items():
            rec = intel.recommendation_for(isin)
            ch = intel.changes.get(isin)
            score_rows.append({
                "Name": sc.name, "Overall": sc.overall,
                "Change": (ch.delta if ch else None),
                **{PILLAR_DEFINITIONS[k][0]: p.score for k, p in sc.pillars.items()},
                "Signal": rec.label if rec else "—",
                "Confidence": rec.confidence if rec else "—",
            })
        df = pd.DataFrame(score_rows).sort_values("Overall", ascending=False, na_position="last")
        st.dataframe(df, width="stretch", hide_index=True,
                     column_config={c: st.column_config.NumberColumn(format="%.0f")
                                    for c in ["Overall", "Change", "Momentum", "Risk",
                                              "Valuation", "Growth", "Quality"]})
        missing = [PILLAR_DEFINITIONS[k][0] for k, p in next(iter(intel.scores.values())).pillars.items()
                   if not p.available]
        if missing:
            st.caption("Empty columns are pillars that need company fundamentals: "
                       + ", ".join(missing) + ".")

        st.subheader("Worth a look")
        actionable = intel.actionable
        if not actionable:
            st.success("Nothing crossed the thresholds you set.")
        for rec in actionable:
            with st.container(border=True):
                a, b = st.columns([4, 1])
                a.markdown(f"**{rec.name}** — {rec.label}")
                b.markdown(f"confidence: {rec.confidence}")
                for reason in rec.reasons:
                    st.markdown(f"- {reason}")
                if rec.invalidators:
                    st.caption("Stops applying when: " + "; ".join(rec.invalidators))
                if rec.risks:
                    st.caption("Bear in mind: " + " ".join(rec.risks))
                st.caption(f"Based on data to {rec.as_of}.")

        with st.expander("Everything else"):
            for rec in [r for r in intel.recommendations if not r.is_actionable]:
                st.markdown(f"**{rec.name}** — {rec.label}: {rec.reasons[0] if rec.reasons else ''}")

        for label, why in unavailable_labels(intel.scores).items():
            st.caption(f"**{label}** is not produced: {why}")

    st.subheader("Alerts")
    events = alert_engine.recent_events(session)
    c1, c2 = st.columns([3, 1])
    c1.caption(f"{len(events)} unread. Thresholds live in the database and can be edited below.")
    if events and c2.button("Mark all read", width="stretch"):
        alert_engine.acknowledge_all(session)
        st.rerun()
    if not events:
        st.success("No unread alerts.")
    for e in events:
        st.markdown(f"- `{e.as_of}` **{e.category}** — {e.message}")

    with st.expander("Alert rules"):
        rules = alert_engine.load_rules(session)
        rule_df = pd.DataFrame([{"Enabled": r.enabled, "Rule": r.label, "Category": r.category,
                                 "Scope": r.scope, "Metric": r.metric, "Operator": r.operator,
                                 "Threshold": float(r.threshold), "Cooldown (days)": r.cooldown_days}
                                for r in rules])
        edited = st.data_editor(rule_df, width="stretch", hide_index=True, key="rules_editor",
                                disabled=["Rule", "Category", "Scope", "Metric"])
        if st.button("Save rules"):
            for r, (_, row) in zip(rules, edited.iterrows()):
                r.enabled = bool(row["Enabled"])
                r.operator = str(row["Operator"])
                r.threshold = row["Threshold"]
                r.cooldown_days = int(row["Cooldown (days)"])
            session.commit()
            st.success("Saved.")
            st.rerun()

# ---------------------------------------------------------------- Scenarios
with tabs[5]:
    an_ok = an is not None and not an.holding_returns.empty
    if not an_ok:
        st.info("Scenarios need price history for at least one holding. Refresh market data first.")
    else:
        weights = pd.Series({h.isin: h.weight for h in snapshot.holdings if h.weight}, dtype=float)
        names_by_isin = {h.isin: (h.name or h.isin) for h in snapshot.holdings}
        base_returns = sim.portfolio_returns(an.holding_returns, weights)
        start_value = snapshot.total_value or float(snapshot.cash)

        st.subheader("A range of possible futures")
        st.caption("This resamples your own history in blocks to keep streaks intact. It is not a "
                   "forecast: it shows what a range of outcomes would look like **if** the "
                   "assumptions below held.")
        c = st.columns(4)
        years = c[0].slider("Years", 1, 20, 5)
        drift_label = c[1].selectbox("Expected return", ["Assume none", "Set my own", "Use my history"],
                                     help="Historical averages over a few years are a poor guide to "
                                          "the future. Starting from zero shows the risk alone.")
        annual = c[2].slider("Annual return", -0.05, 0.15, 0.05, 0.01, format="%.0f%%",
                             disabled=drift_label != "Set my own")
        monthly = c[3].number_input("Monthly contribution €", 0, 5000, 0, 50)
        drift = {"Assume none": "zero", "Set my own": "fixed", "Use my history": "historical"}[drift_label]

        result = sim.simulate_portfolio(base_returns, start_value, horizon_years=years, paths=2000,
                                        drift=drift, annual_drift=annual, monthly_contribution=monthly)
        if result is None:
            st.warning("Not enough price history to simulate. At least 60 trading days are needed.")
        else:
            invested_line = pd.Series(
                start_value + monthly * (result.percentile_paths.index * 12),
                index=result.percentile_paths.index)
            st.plotly_chart(charts.fan_chart(result.percentile_paths, start_value, theme,
                                             invested=invested_line),
                            width="stretch", key="fan")
            m = st.columns(4)
            m[0].metric("Median outcome", eur(result.median, 0))
            m[1].metric("Pessimistic (5th percentile)", eur(result.percentile(5), 0))
            m[2].metric("Optimistic (95th percentile)", eur(result.percentile(95), 0))
            m[3].metric("Chance of ending below money in", pct(result.probability_of_loss, 0, sign=False))
            st.caption("Assumptions: " + result.assumptions.describe() + ".")
            for n in result.notes:
                st.warning(n)
            left, right = st.columns([2, 3])
            with left:
                st.dataframe(result.summary(), width="stretch", hide_index=True,
                             column_config={"Value": st.column_config.NumberColumn(format="€ %.0f"),
                                            "Total return": st.column_config.NumberColumn(format="percent")})
            with right:
                st.plotly_chart(charts.outcome_distribution(result.terminal, result.invested, theme),
                                width="stretch", key="dist")

        st.divider()
        st.subheader("What if the market fell")
        betas = pd.Series({h.isin: None for h in snapshot.holdings}, dtype=float)
        if not intel.metrics.empty and "beta" in intel.metrics.columns:
            betas = intel.metrics["beta"]
        shock_rows = []
        for size in (-0.10, -0.20, -0.30, -0.40):
            s_res = sim.uniform_shock(weights, betas, size)
            shock_rows.append({"Scenario": s_res.label, "Portfolio": s_res.portfolio_change,
                               "Value after": (start_value * (1 + s_res.portfolio_change))})
        sector_series = pd.Series({h.isin: (ETF_BUCKET if h.asset_type == "etf" else (h.sector or "Unknown"))
                                   for h in snapshot.holdings})
        for group in [g for g in sector_series.unique() if g != "Unknown"][:4]:
            s_res = sim.group_shock(weights, sector_series, group, -0.40)
            shock_rows.append({"Scenario": s_res.label, "Portfolio": s_res.portfolio_change,
                               "Value after": (start_value * (1 + s_res.portfolio_change))})
        st.dataframe(pd.DataFrame(shock_rows), width="stretch", hide_index=True,
                     column_config={"Portfolio": st.column_config.NumberColumn(format="percent"),
                                    "Value after": st.column_config.NumberColumn(format="€ %.0f")})
        st.caption("Market falls are passed through each holding's beta and ignore company-specific "
                   "news, so a real crash would not look exactly like this.")

        worst = sim.worst_observed(base_returns)
        if not worst.empty:
            st.markdown("**The worst stretches this portfolio has actually lived through**")
            st.dataframe(worst, width="stretch", hide_index=True,
                         column_config={"Worst": st.column_config.NumberColumn(format="percent")})
            st.caption("Limited by a short history: the worst thing that has happened is rarely the "
                       "worst thing that can.")

        st.divider()
        st.subheader("What if the mix were different")
        cov = an.holding_returns.dropna(how="all").cov() * 252
        cov = cov.loc[[i for i in cov.index if i in weights.index],
                      [c for c in cov.columns if c in weights.index]]
        alternatives = {"As it is now": weights}
        if len(cov) >= 2:
            alternatives["Equal weights"] = sim.equal_weights(list(cov.index))
            alternatives["Minimum variance"] = sim.minimum_variance_weights(cov)
            alternatives["Equal risk"] = sim.risk_parity_weights(cov)
        rows = []
        for label, w in alternatives.items():
            r = sim.portfolio_returns(an.holding_returns, w)
            if r.empty:
                continue
            from bux_analyser.analytics.risk import annualised_volatility, max_drawdown, sharpe_ratio
            lvl = (1 + r).cumprod()
            dd = max_drawdown(lvl)
            rows.append({"Allocation": label, "Return over the period": float(lvl.iloc[-1] - 1),
                         "Volatility": annualised_volatility(r), "Sharpe": sharpe_ratio(r),
                         "Worst fall": dd.max_drawdown if dd else None,
                         "Largest position": float(w.max()) if len(w) else None})
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     column_config={c: st.column_config.NumberColumn(format="percent")
                                    for c in ["Return over the period", "Volatility", "Worst fall",
                                              "Largest position"]})
        st.caption("These are what the alternatives **would have done** over the history you hold, "
                   "using today's list of holdings. A mix that looks better in the past has not been "
                   "shown to do better next year, and this comparison ignores the tax and dealing "
                   "costs of getting there. Minimum variance and equal risk are shown because "
                   "neither needs a forecast of returns.")

# ---------------------------------------------------------------- Holdings
with tabs[6]:
    names = [h.name for h in snapshot.holdings]
    if not names:
        st.info("No open positions.")
    else:
        pick = st.selectbox("Holding", names)
        h = next(x for x in snapshot.holdings if x.name == pick)
        c = st.columns(5)
        c[0].metric("Value", eur(h.value_base))
        c[1].metric("Unrealised P/L", eur(h.unrealized_base), pct(h.unrealized_pct))
        c[2].metric("Quantity", num(h.quantity, 6, dash="0"))
        c[3].metric("Average cost", f"{num(h.avg_cost_local, 4)} {h.currency}")
        c[4].metric("Weight", pct(h.weight, 1, sign=False))

        if h.decomposition:
            d = h.decomposition
            if d.is_single_currency:
                st.caption(f"Traded in euros, so the whole {pct(d.total)} comes from the share price.")
            else:
                st.caption(f"Of the {pct(d.total)} return on cost, the share price contributed "
                           f"{pct(d.local)} and the {h.currency} exchange rate {pct(d.fx)}.")

        c = st.columns(4)
        c[0].metric("Dividends received", eur(h.dividends_base))
        c[1].metric("Fees and taxes", eur(h.fees_base + h.taxes_base))
        c[2].metric("Realised P/L", eur(h.realized_pl_base))
        c[3].metric("Sector", (ETF_BUCKET if h.asset_type == "etf" else (h.sector or "unknown")))

        metrics = (intel.fundamentals or {}).get(h.isin)
        if metrics is None:
            st.info("No financial statements are cached for this holding. Free filings cover "
                    "US-listed companies; European names rely on a best-effort source that "
                    "often returns nothing.")
        else:
            st.markdown(f"**Financials** — period ending {metrics.period_end}"
                        + (f", filed {metrics.reported_at}" if metrics.reported_at else ""))
            groups = {"Valuation": ["price_to_earnings", "ev_to_ebit", "price_to_sales",
                                    "price_to_book", "fcf_yield", "earnings_yield"],
                      "Growth": ["revenue_growth_1y", "revenue_cagr_3y", "earnings_cagr_3y",
                                 "fcf_cagr_3y"],
                      "Quality": ["return_on_equity", "return_on_capital", "gross_margin",
                                  "operating_margin", "net_margin", "fcf_conversion",
                                  "debt_to_equity", "net_debt_to_ebit"]}
            cols = st.columns(3)
            for col, (title, keys) in zip(cols, groups.items()):
                with col:
                    st.markdown(f"*{title}*")
                    for key in keys:
                        value = metrics.get(key)
                        if value is None:
                            continue
                        label = key.replace("_", " ").capitalize()
                        shown = (f"{value:.1%}" if key.endswith(("yield", "margin", "growth",
                                                                 "cagr_3y", "conversion",
                                                                 "on_equity", "on_capital"))
                                 else f"{value:,.2f}")
                        st.markdown(f"{label}: **{shown}**")
            for n in metrics.notes:
                st.caption(n)
            st.caption(f"Source: {metrics.quality} statements"
                       + (f", filed {metrics.reported_at}." if metrics.reported_at else "."))

        for i in h.issues:
            st.warning(i)

        series, _ = store.price_history(h.isin, snapshot.history.index[0].date(), refresh=False) \
            if not snapshot.history.empty else (pd.Series(dtype=float), None)
        st.plotly_chart(charts.price_comparison({h.name: series}, theme), width="stretch", key="holding_price")
        st.caption(f"Price in {h.price_currency or h.currency}, rebased to 100. "
                   "Currency effects are not included in this line.")

        with st.expander("Where these numbers come from"):
            st.markdown(
                f"**Price** — {provenance_line(h.price_provenance)}  \n"
                f"**Exchange rate** — {provenance_line(h.fx_provenance)}  \n"
                f"**Symbol** — `{h.ticker or 'unresolved'}` matched from ISIN `{h.isin}`  \n"
                "**Quantity, average cost, dividends, fees, taxes** — calculated from your BUX export "
                "using the average-cost method")

        st.subheader("Compare")
        others = st.multiselect("Holdings to compare", names,
                                default=names[:min(4, len(names))])
        if others and not snapshot.history.empty:
            start = snapshot.history.index[0].date()
            picked = {}
            for n in others:
                hh = next(x for x in snapshot.holdings if x.name == n)
                s, _ = store.price_history(hh.isin, start, refresh=False)
                picked[n] = s
            st.plotly_chart(charts.price_comparison(picked, theme), width="stretch", key="compare")


# ---------------------------------------------------------------- Research
with tabs[7]:
    st.subheader("Does a rule actually work?")
    st.caption("This replays a rule over the price history you hold, trading only on what was "
               "known at the time. Read the limits below before believing any number here.")
    if an is None or an.eur_prices.empty or an.eur_prices.shape[1] < 2:
        st.info("Backtesting needs price history for at least two holdings.")
    else:
        prices = an.eur_prices.dropna(how="all")
        names = {h.isin: (h.name or h.isin) for h in snapshot.holdings}
        c = st.columns(4)
        strategy_name = c[0].selectbox("Rule", ["Momentum", "Low volatility", "Equal weight"])
        positions = c[1].slider("Hold at most", 1, max(2, prices.shape[1]),
                                min(5, prices.shape[1]))
        frequency = c[2].selectbox("Rebalance", ["Monthly", "Quarterly", "Yearly"], index=1)
        cost = c[3].slider("Cost per trade", 0.0, 1.0, 0.25, 0.05, format="%.2f%%",
                           help="Charged on the value traded, covering fees and the spread.")
        config = bt.BacktestConfig(rebalance={"Monthly": "ME", "Quarterly": "QE", "Yearly": "YE"}[frequency],
                                   cost_bps=cost * 100, max_positions=positions)
        signal = {"Momentum": bt.momentum_signal(), "Low volatility": bt.low_volatility_signal(),
                  "Equal weight": bt.equal_weight_signal()}[strategy_name]

        if st.button("Run the backtest", type="primary"):
            with st.spinner("Replaying history and testing it against chance…"):
                strategy = bt.run(prices, signal, config, label=strategy_name)
                hold = bt.buy_and_hold(prices, config)
                verdict = bt.permutation_test(prices, signal, config, runs=120)
            st.session_state["backtest"] = (strategy, hold, verdict, strategy_name)

        if "backtest" in st.session_state:
            strategy, hold, verdict, label = st.session_state["backtest"]
            if strategy is None:
                st.warning("Not enough history to run this. A year of prices is needed before the "
                           "first trade.")
            else:
                results = [r for r in (strategy, hold) if r is not None]
                table = bt.compare(results)
                st.plotly_chart(charts.price_comparison(
                    {r.label: r.equity for r in results}, theme), width="stretch", key="bt_curve")
                st.dataframe(table.rename(columns=lambda c: c.replace("_", " ").capitalize()),
                             width="stretch",
                             column_config={c: st.column_config.NumberColumn(format="percent")
                                            for c in ["Total return", "Cagr", "Volatility",
                                                      "Max drawdown", "Win rate", "Costs paid"]})
                p = verdict.get("p_value")
                if p is None:
                    st.info("The significance test could not run on this history.")
                elif p > 0.10:
                    st.warning(f"Shuffling the signal at random beat this result {p:.0%} of the "
                               f"time. On this data the rule shows no skill: the outcome is "
                               f"consistent with luck.")
                else:
                    st.success(f"Random orderings of the same signal beat this result only "
                               f"{p:.0%} of the time. That is suggestive, not proof, and it is "
                               f"one test on one short history.")
                st.caption(f"Sharpe {verdict.get('actual_sharpe', float('nan')):.2f} against a "
                           f"median of {verdict.get('null_median', float('nan')):.2f} from "
                           f"{verdict.get('runs', 0)} random runs.")
                with st.expander("What this backtest cannot tell you", expanded=True):
                    for bias in strategy.biases:
                        st.markdown(f"- {bias}")

    st.divider()
    st.subheader("What kind of bets is this portfolio making?")
    if an is None or an.portfolio_returns.empty:
        st.info("Factor analysis needs return history.")
    else:
        if st.button("Fetch factor data and analyse"):
            with st.spinner("Downloading the Kenneth French factor data…"):
                provider = fa.FrenchFactorProvider()
                frame = provider.factors("developed")
                st.session_state["factors"] = frame
        frame = st.session_state.get("factors")
        if frame is None:
            st.caption("Factor returns come from the Kenneth French data library, which is free "
                       "and updated periodically. Nothing is sent: only the public file is fetched.")
        elif frame.empty:
            st.warning("The factor file could not be read.")
        else:
            rf = frame["RF"] if "RF" in frame.columns else 0.0
            excess = an.portfolio_returns - (rf if isinstance(rf, float) else rf.reindex(
                an.portfolio_returns.index).fillna(0.0))
            columns = [c for c in frame.columns if c != "RF"]
            exposure = fa.regress(excess, frame[columns], dataset="developed")
            if exposure is None:
                st.warning("Not enough overlap between your history and the factor data.")
            else:
                for line in exposure.describe():
                    st.markdown(f"- {line}")
                c = st.columns(3)
                c[0].metric("Explained by factors", pct(exposure.r_squared, 0, sign=False))
                c[1].metric("Alpha a year", pct(exposure.alpha_annual, 1))
                c[2].metric("Days analysed", f"{exposure.n_obs:,}")
                st.dataframe(pd.DataFrame({
                    "Factor": [fa.FACTOR_LABELS.get(k, k) for k in exposure.loadings],
                    "Loading": list(exposure.loadings.values()),
                    "t-statistic": [exposure.t_statistics[k] for k in exposure.loadings]}),
                    width="stretch", hide_index=True)
                st.caption("A t-statistic beyond about 2 means the tilt is unlikely to be noise. "
                           "These describe how the portfolio has behaved; they do not predict.")
                for n in exposure.notes:
                    st.caption(n)

    st.divider()
    st.subheader("Ask about the portfolio")
    tools = PortfolioTools(snapshot, intel)
    holding_names = [h.name for h in snapshot.holdings]
    questions = {
        "How is the portfolio doing?": ("get_portfolio_summary", {}),
        "How am I doing against the market?": ("get_performance", {}),
        "What are my biggest risks?": ("get_risk_metrics", {}),
        "Which holdings carry the most risk?": ("get_risk_contributors", {}),
        "Am I overexposed to one sector?": ("get_allocation", {"dimension": "sector"}),
        "What is my currency exposure?": ("get_allocation", {"dimension": "currency"}),
        "What has crossed an alert?": ("get_alerts", {}),
        "Tell me about one holding": ("get_position", {"query": None}),
        "Why does a holding have its signal?": ("get_recommendation_reasons", {"query": None}),
        "What do the financials say?": ("get_fundamentals", {"query": None}),
    }
    chosen = st.selectbox("Question", list(questions))
    function, kwargs = questions[chosen]
    kwargs = dict(kwargs)
    if "query" in kwargs and holding_names:
        kwargs["query"] = st.selectbox("Holding", holding_names, key="ask_holding")
    answer = tools.call(function, **kwargs)
    st.markdown(f"**{answer.summary}**")
    if answer.table is not None and not answer.table.empty:
        st.dataframe(answer.table, width="stretch")
    for caveat in answer.caveats:
        st.caption(f"Note: {caveat}")
    if answer.sources:
        st.caption("Based on: " + "; ".join(answer.sources))
    st.caption("These answers are produced by the application's own calculations, not by a "
               "language model. Every figure comes from your data.")

# ---------------------------------------------------------------- Transactions
with tabs[8]:
    from bux_analyser.importers.persist import load_txns
    txns = load_txns(session)
    tx_df = pd.DataFrame([{
        "Date": t.date, "Type": t.type.value, "Name": t.name or "—", "ISIN": t.isin or "—",
        "Quantity": float(t.quantity) or None, "Price": float(t.price) or None, "Ccy": t.currency,
        "FX to EUR": float(t.fx_rate), "Cash effect €": float(t.cash_effect_base), "Note": t.note,
    } for t in sorted(txns, key=lambda x: x.date, reverse=True)])
    kinds = sorted(tx_df["Type"].unique()) if not tx_df.empty else []
    chosen = st.multiselect("Type", kinds, default=kinds)
    view = tx_df[tx_df["Type"].isin(chosen)] if chosen else tx_df
    st.dataframe(view, width="stretch", hide_index=True,
                 column_config={"Cash effect €": st.column_config.NumberColumn(format="€ %.2f"),
                                "Quantity": st.column_config.NumberColumn(format="%.6f"),
                                "Price": st.column_config.NumberColumn(format="%.4f"),
                                "FX to EUR": st.column_config.NumberColumn(format="%.6f")})
    st.caption(f"{len(tx_df)} transactions. Every row was derived from your BUX export; "
               "the original rows are kept in the database.")

st.caption(f"Snapshot {snapshot.as_of:%Y-%m-%d %H:%M} UTC · base currency EUR · "
           "figures derived from your BUX export and cached public market data.")
