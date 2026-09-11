"""Streamlit dashboard. Run: streamlit run app.py   (everything stays on this machine)"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from bux_analyser.db import make_engine, session_factory
from bux_analyser.importers.persist import import_bux_file
from bux_analyser.service import build_snapshot, default_store, refresh_market_data

st.set_page_config(page_title="BUX Analyser", layout="wide")


@st.cache_resource
def _session():
    return session_factory(make_engine())()


session = _session()
store = default_store(session)


def eur(x, digits=2):
    return "n/a" if x is None or pd.isna(x) else f"€ {x:,.{digits}f}"


def pct(x):
    return "n/a" if x is None or pd.isna(x) else f"{x:+.2%}"


def prov_text(p):
    if p is None:
        return "unavailable"
    d = p.data_date.isoformat() if p.data_date else "?"
    return f"{p.source} · data {d} · retrieved {p.retrieved_at:%Y-%m-%d %H:%M} UTC · {p.kind}{(' · ' + p.note) if p.note else ''}"


# ---------------- sidebar: import / refresh / health ----------------
with st.sidebar:
    st.header("Data")
    up = st.file_uploader("BUX transaction-history CSV", type=["csv"])
    if up is not None and st.button("Import file"):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / up.name
            p.write_bytes(up.getvalue())
            r = import_bux_file(session, p)
        st.success(f"{r.rows} rows, {r.new_rows} new, {r.new_txns} transactions" + (" (already imported)" if r.already_imported else ""))
        st.write("Cash reconciliation:", "✅ matches BUX balances" if r.cash_ok else "❌ mismatch")
        for m in r.cash_messages:
            st.error(m)
        for w in r.warnings:
            st.warning(w)
    if st.button("Refresh market data"):
        with st.spinner("Fetching prices and FX rates…"):
            for w in refresh_market_data(session, store):
                st.warning(w)
        st.rerun()
    st.caption("Sources: Yahoo Finance (prices, delayed quotes), ECB via frankfurter.dev (FX). "
               "Only public symbols are requested; your transactions never leave this machine.")
    with st.expander("Provider health"):
        for h in store.router.health():
            st.write(f"**{h.name}** · {h.calls} calls · {h.failures} failures")
            if h.last_error:
                st.caption(h.last_error)

snap = build_snapshot(session, store, refresh=False)
led = snap.ledger

if not led.positions and led.cash_base == 0:
    st.info("No data yet. Import a BUX transaction-history CSV from the sidebar.")
    st.stop()

# ---------------- overview ----------------
st.title("Portfolio")
for w in snap.warnings:
    st.warning(w)

invested = float(led.net_invested_base)
total_pl = (snap.total_value - invested) if snap.total_value is not None else None
c = st.columns(6)
c[0].metric("Total value", eur(snap.total_value), pct(snap.day_change_pct) if snap.day_change_pct is not None else None,
            help="Securities at latest price × ECB rate + cash. Delta = today's change.")
c[1].metric("Today", eur(snap.day_change), help="Sum of quantity × (price − previous close) × FX, holdings with a quote only.")
c[2].metric("Net invested", eur(invested), help="Deposits − withdrawals.")
c[3].metric("Total P/L", eur(total_pl), pct(total_pl / invested) if (total_pl is not None and invested) else None,
            help="Total value − net invested. Includes realized P/L, dividends, interest, fees and taxes.")
c[4].metric("Unrealized P/L", eur(snap.unrealized), help="Value − gross cost of open positions (fees excluded, as in BUX).")
c[5].metric("Cash", eur(snap.cash), help="Reconciled against BUX's running balance at import.")

c = st.columns(6)
c[0].metric("Realized P/L", eur(float(led.realized_pl_base)))
c[1].metric("Dividends (net)", eur(float(led.dividends_net_base)))
c[2].metric("Interest + other income", eur(float(led.interest_base + led.income_base)))
c[3].metric("Fees", eur(-float(led.fees_base)))
c[4].metric("Taxes", eur(-float(led.taxes_base)), help="Transaction taxes (TOB/FTT) + dividend withholding.")
c[5].metric("Money-weighted return (XIRR)", pct(snap.mwr), help="Annualised, from deposits/withdrawals and current value.")

if not snap.twr.empty:
    st.caption(f"Time-weighted return since {snap.history.index[0].date()}: **{snap.twr.iloc[-1] - 1:+.2%}** "
               f"(price data: {'complete' if snap.history['total'].notna().all() else 'gaps on some days'})")

# ---------------- holdings ----------------
st.subheader("Holdings")
rows = []
for h in snap.holdings:
    rows.append({"Name": h.name, "ISIN": h.isin, "Symbol": h.ticker or "—", "Qty": float(h.quantity),
                 "Avg cost": f"{float(h.avg_cost_local):,.2f} {h.currency}",
                 "Price": (f"{h.price:,.2f} {h.price_currency}" if h.price is not None else "n/a"),
                 "Value €": h.value_base, "P/L €": h.unrealized_base, "P/L %": h.unrealized_pct,
                 "Today %": h.day_change_pct, "Weight": h.weight, "Dividends €": float(h.dividends_base),
                 "Costs €": float(h.fees_base + h.taxes_base), "Issues": "; ".join(h.issues)})
df = pd.DataFrame(rows).sort_values("Value €", ascending=False, na_position="last")
st.dataframe(df, width='stretch', hide_index=True,
             column_config={"Value €": st.column_config.NumberColumn(format="€ %.2f"),
                            "P/L €": st.column_config.NumberColumn(format="€ %.2f"),
                            "P/L %": st.column_config.NumberColumn(format="%.2f%%") if False else st.column_config.ProgressColumn(format="%.1f%%", min_value=-1, max_value=1),
                            "Today %": st.column_config.NumberColumn(format="%.2f"),
                            "Weight": st.column_config.NumberColumn(format="%.1f%%") if False else st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=1),
                            "Dividends €": st.column_config.NumberColumn(format="€ %.2f"),
                            "Costs €": st.column_config.NumberColumn(format="€ %.2f"),
                            "Qty": st.column_config.NumberColumn(format="%.6f")})

with st.expander("Where does each number come from?"):
    for h in snap.holdings:
        st.markdown(f"**{h.name}** ({h.isin}, symbol `{h.ticker or '—'}`)  \n"
                    f"Price: {prov_text(h.price_provenance)}  \nFX: {prov_text(h.fx_provenance)}  \n"
                    f"Quantity, average cost, dividends, fees: BUX export (calculated by ledger, average-cost method)")

# ---------------- charts ----------------
left, right = st.columns([1, 1])
with left:
    st.subheader("Allocation")
    alloc = df.dropna(subset=["Value €"])
    if snap.cash > 0:
        alloc = pd.concat([alloc, pd.DataFrame([{"Name": "Cash", "Value €": snap.cash}])])
    if not alloc.empty:
        fig = px.pie(alloc, names="Name", values="Value €", hole=0.45)
        fig.update_traces(textposition="inside", textinfo="percent+label")
        fig.update_layout(showlegend=False, margin=dict(t=10, b=10, l=10, r=10), height=420)
        st.plotly_chart(fig, width='stretch')
    else:
        st.info("Allocation needs prices. Click “Refresh market data”.")
with right:
    st.subheader("Portfolio value")
    hist = snap.history
    if not hist.empty and hist["total"].notna().any():
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hist.index, y=hist["total"], name="Total value", line=dict(width=2)))
        fig.add_trace(go.Scatter(x=hist.index, y=hist["net_invested"], name="Net invested", line=dict(dash="dot")))
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=420, legend=dict(orientation="h"), yaxis_title="€")
        st.plotly_chart(fig, width='stretch')
        if not snap.twr.empty:
            st.caption("Time-weighted index (1.0 = start): a measure of how the *investments* did, independent of when you deposited.")
            fig2 = go.Figure(go.Scatter(x=snap.twr.index, y=snap.twr, name="TWR index"))
            fig2.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=220)
            st.plotly_chart(fig2, width='stretch')
    else:
        st.info("Value history needs price history. Click “Refresh market data”.")

st.subheader("Holding performance (price, rebased to 100)")
choices = [h for h in snap.holdings if h.ticker]
sel = st.multiselect("Holdings", [h.name for h in choices], default=[h.name for h in choices[:6]])
if sel:
    fig = go.Figure()
    for h in choices:
        if h.name not in sel:
            continue
        s, _ = store.price_history(h.isin, snap.history.index[0].date(), refresh=False)
        if s.empty:
            continue
        fig.add_trace(go.Scatter(x=s.index, y=s / s.iloc[0] * 100, name=h.name))
    fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=420, legend=dict(orientation="h"))
    st.plotly_chart(fig, width='stretch')
    st.caption("Local-currency prices from the cache; FX effects are not included in this chart.")

st.caption(f"Snapshot {snap.as_of:%Y-%m-%d %H:%M} UTC · base currency EUR · all figures derived from your BUX export and cached public data.")
