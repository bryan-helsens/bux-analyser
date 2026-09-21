"""BUX Analyser dashboard. Run: streamlit run app.py

Everything stays on this machine. Only public symbols are sent to market-data
sources; transactions, quantities and amounts never leave the local database.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from bux_analyser.analytics.portfolio import ETF_BUCKET
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

tabs = st.tabs(["Overview", "Performance", "Exposure", "Risk", "Holdings", "Transactions"])

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

# ---------------------------------------------------------------- Holdings
with tabs[4]:
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

# ---------------------------------------------------------------- Transactions
with tabs[5]:
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
