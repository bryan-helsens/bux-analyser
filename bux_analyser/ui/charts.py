"""Chart builders. Each takes prepared data and returns a Plotly figure, so the
dashboard file stays about layout and these stay testable without Streamlit."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .theme import MAX_SERIES, Theme

EUR = "€%{y:,.0f}"


def _empty(theme: Theme, message: str, height: int = 240) -> go.Figure:
    """A chart with nothing to show says why, instead of rendering an empty box."""
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font=dict(color=theme.muted, size=13),
                       xref="paper", yref="paper", x=0.5, y=0.5)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return theme.apply(fig, height=height, legend=False)


def value_over_time(history: pd.DataFrame, theme: Theme) -> go.Figure:
    """Portfolio value against the capital actually put in."""
    if history is None or history.empty or history["total"].notna().sum() < 2:
        return _empty(theme, "No value history yet. Refresh market data.")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=history.index, y=history["net_invested"], name="Net invested",
                             line=dict(color=theme.muted, width=2, dash="dot"),
                             hovertemplate="Net invested " + EUR + "<extra></extra>"))
    fig.add_trace(go.Scatter(x=history.index, y=history["total"], name="Portfolio value",
                             line=dict(color=theme.color(0), width=2),
                             hovertemplate="Value " + EUR + "<extra></extra>"))
    return theme.apply(fig, height=380, hovermode="x unified", yaxis_title=None)


def benchmark_comparison(portfolio_index: pd.Series, benchmarks, theme: Theme) -> go.Figure:
    """Growth of 100 invested, portfolio against benchmark proxies."""
    if portfolio_index is None or portfolio_index.dropna().empty:
        return _empty(theme, "No performance history yet.")
    p = portfolio_index.dropna()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=p.index, y=p / p.iloc[0] * 100, name="My portfolio",
                             line=dict(color=theme.color(0), width=2.5),
                             hovertemplate="Portfolio %{y:,.1f}<extra></extra>"))
    for i, b in enumerate(benchmarks[:MAX_SERIES - 1], start=1):
        fig.add_trace(go.Scatter(x=b.index.index, y=b.index * 100, name=b.label,
                                 line=dict(color=theme.color(i), width=2),
                                 hovertemplate=b.label + " %{y:,.1f}<extra></extra>"))
    return theme.apply(fig, height=400, hovermode="x unified", yaxis_title=None)


def horizontal_bars(values: pd.Series, theme: Theme, *, money: bool = False,
                    color: str | None = None, height: int | None = None,
                    max_items: int = 12) -> go.Figure:
    """One measure across categories, sorted. Long tails fold into 'Other' rather
    than adding hues that cannot be told apart."""
    if values is None or values.empty:
        return _empty(theme, "Nothing to show yet.")
    s = values.sort_values(ascending=False)
    if len(s) > max_items:
        s = pd.concat([s.iloc[:max_items - 1], pd.Series({"Other": s.iloc[max_items - 1:].sum()})])
    s = s.sort_values()
    fmt = "€%{x:,.0f}" if money else "%{x:.1%}"
    fig = go.Figure(go.Bar(
        x=s.to_numpy(), y=list(s.index), orientation="h",
        marker=dict(color=color or theme.color(0), cornerradius=4),
        text=[(f"€{v:,.0f}" if money else f"{v:.1%}") for v in s],
        textposition="outside", textfont=dict(color=theme.muted, size=11),
        hovertemplate="%{y}: " + fmt + "<extra></extra>"))
    fig.update_xaxes(showticklabels=False, showgrid=False)
    fig.update_yaxes(showgrid=False)
    return theme.apply(fig, height=height or max(220, 30 * len(s) + 60), legend=False)


def grouped_bars(frame: pd.DataFrame, theme: Theme, *, percent: bool = True,
                 height: int | None = None) -> go.Figure:
    """Two or three measures per category, e.g. weight against risk contribution."""
    if frame is None or frame.empty:
        return _empty(theme, "Not enough data yet.")
    fig = go.Figure()
    fmt = "%{x:.1%}" if percent else "%{x:,.2f}"
    for i, col in enumerate(frame.columns):
        fig.add_trace(go.Bar(y=list(frame.index), x=frame[col].to_numpy(), name=str(col),
                             orientation="h", marker=dict(color=theme.color(i), cornerradius=4),
                             hovertemplate="%{y} " + str(col) + ": " + fmt + "<extra></extra>"))
    fig.update_layout(barmode="group", bargap=0.25, bargroupgap=0.08)
    fig.update_xaxes(tickformat=".0%" if percent else None, showgrid=True, gridcolor=theme.grid)
    fig.update_yaxes(showgrid=False)
    return theme.apply(fig, height=height or max(260, 34 * len(frame) + 80))


def returns_heatmap(table: pd.DataFrame, theme: Theme | None = None) -> go.Figure:
    """Monthly returns by year. Diverging scale centred on zero."""
    theme = theme or Theme()
    if table is None or table.empty:
        return _empty(theme, "Not a full month of history yet.")
    z = table.to_numpy(dtype=float)
    limit = float(np.nanmax(np.abs(z))) or 0.01
    fig = go.Figure(go.Heatmap(
        z=z, x=list(table.columns), y=[str(i) for i in table.index],
        colorscale=theme.diverging, zmid=0, zmin=-limit, zmax=limit,
        xgap=2, ygap=2, showscale=False,
        text=[[("" if pd.isna(v) else f"{v:+.1%}") for v in row] for row in z],
        texttemplate="%{text}", textfont=dict(size=10),
        hovertemplate="%{y} %{x}: %{z:+.2%}<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(side="top", showgrid=False)
    return theme.apply(fig, height=max(180, 42 * len(table) + 90), legend=False)


def correlation_heatmap(corr: pd.DataFrame, theme: Theme) -> go.Figure:
    """Pairwise correlation of daily euro returns."""
    if corr is None or corr.empty or len(corr) < 2:
        return _empty(theme, "Need at least two holdings with price history.")
    order = corr.mean(axis=1).sort_values(ascending=False).index
    c = corr.loc[order, order]
    fig = go.Figure(go.Heatmap(
        z=c.to_numpy(), x=list(c.columns), y=list(c.index),
        colorscale=theme.diverging, zmid=0, zmin=-1, zmax=1, xgap=2, ygap=2,
        colorbar=dict(thickness=10, tickfont=dict(color=theme.muted, size=10), outlinewidth=0),
        hovertemplate="%{y} vs %{x}: %{z:.2f}<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(showgrid=False, tickangle=-40)
    return theme.apply(fig, height=max(320, 26 * len(c) + 160), legend=False)


def drawdown_area(level: pd.Series, theme: Theme) -> go.Figure:
    """How far below its previous peak the portfolio has been."""
    if level is None or level.dropna().empty:
        return _empty(theme, "No history yet.")
    lv = level.dropna().astype(float)
    dd = lv / lv.cummax() - 1.0
    fig = go.Figure(go.Scatter(x=dd.index, y=dd, name="Drawdown", fill="tozeroy",
                               line=dict(color=theme.negative, width=1.5),
                               fillcolor="rgba(227,73,72,0.18)",
                               hovertemplate="%{y:.1%}<extra></extra>"))
    fig.update_yaxes(tickformat=".0%")
    return theme.apply(fig, height=260, legend=False, hovermode="x unified")


def decomposition_bars(rows: pd.DataFrame, theme: Theme) -> go.Figure:
    """Per holding: how much of the return came from the share price and how much
    from the exchange rate."""
    if rows is None or rows.empty:
        return _empty(theme, "Needs prices and a purchase history.")
    fig = go.Figure()
    for i, (col, label) in enumerate([("local", "Share price"), ("fx", "Currency")]):
        fig.add_trace(go.Bar(y=list(rows.index), x=rows[col].to_numpy(), name=label,
                             orientation="h", marker=dict(color=theme.color(i), cornerradius=4),
                             hovertemplate="%{y} " + label + ": %{x:+.1%}<extra></extra>"))
    fig.update_layout(barmode="relative", bargap=0.3)
    fig.update_xaxes(tickformat="+.0%", showgrid=True, gridcolor=theme.grid, zeroline=True,
                     zerolinecolor=theme.muted, zerolinewidth=1)
    fig.update_yaxes(showgrid=False)
    return theme.apply(fig, height=max(260, 32 * len(rows) + 90))


def price_comparison(series: dict[str, pd.Series], theme: Theme) -> go.Figure:
    """Several holdings rebased to 100 at the start of the window."""
    live = {k: v.dropna() for k, v in series.items() if v is not None and len(v.dropna()) > 1}
    if not live:
        return _empty(theme, "Select holdings with price history.")
    fig = go.Figure()
    for i, (name, s) in enumerate(list(live.items())[:MAX_SERIES]):
        fig.add_trace(go.Scatter(x=s.index, y=s / s.iloc[0] * 100, name=name,
                                 line=dict(color=theme.color(i), width=2),
                                 hovertemplate=name + " %{y:,.1f}<extra></extra>"))
    return theme.apply(fig, height=400, hovermode="x unified")


def fan_chart(percentile_paths: pd.DataFrame, start_value: float, theme: Theme,
              invested: pd.Series | None = None) -> go.Figure:
    """The range of simulated outcomes over time.

    Bands, not a line: the middle path is one outcome among many and is not a forecast.
    """
    if percentile_paths is None or percentile_paths.empty:
        return _empty(theme, "Run a simulation to see a range of outcomes.")
    x = percentile_paths.index
    fig = go.Figure()
    bands = [("p5", "p95", 0.12, "5th to 95th percentile"),
             ("p25", "p75", 0.25, "25th to 75th percentile")]
    base = theme.color(0)
    for lo, hi, alpha, label in bands:
        fig.add_trace(go.Scatter(x=x, y=percentile_paths[hi], line=dict(width=0),
                                 showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=x, y=percentile_paths[lo], line=dict(width=0), fill="tonexty",
                                 fillcolor=_rgba(base, alpha), name=label,
                                 hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=percentile_paths["p50"], name="Median outcome",
                             line=dict(color=base, width=2),
                             hovertemplate="Year %{x:.1f}: €%{y:,.0f}<extra></extra>"))
    if invested is not None and len(invested):
        fig.add_trace(go.Scatter(x=x, y=invested, name="Money put in",
                                 line=dict(color=theme.muted, width=2, dash="dot"),
                                 hovertemplate="Put in €%{y:,.0f}<extra></extra>"))
    fig.update_xaxes(title="years from now", showgrid=False)
    fig.update_yaxes(tickprefix="€", separatethousands=True)
    return theme.apply(fig, height=400, hovermode="x unified")


def outcome_distribution(terminal: np.ndarray, invested: float, theme: Theme) -> go.Figure:
    """Where the simulated paths ended up, against the money put in."""
    if terminal is None or not len(terminal):
        return _empty(theme, "Run a simulation first.")
    fig = go.Figure(go.Histogram(x=terminal, nbinsx=60,
                                 marker=dict(color=_rgba(theme.color(0), 0.75)),
                                 hovertemplate="€%{x:,.0f}: %{y} paths<extra></extra>"))
    fig.add_vline(x=invested, line=dict(color=theme.muted, width=2, dash="dot"),
                  annotation_text="money put in", annotation_position="top",
                  annotation_font=dict(color=theme.muted, size=11))
    fig.update_xaxes(tickprefix="€", separatethousands=True, showgrid=False)
    fig.update_yaxes(title="paths")
    return theme.apply(fig, height=300, legend=False)


def score_bars(pillars: dict[str, float], theme: Theme, height: int = 220) -> go.Figure:
    """Pillar scores for one holding, 0 to 100."""
    if not pillars:
        return _empty(theme, "No pillar could be scored.", height=height)
    s = pd.Series(pillars)
    fig = go.Figure(go.Bar(x=s.to_numpy(), y=list(s.index), orientation="h",
                           marker=dict(color=theme.color(0), cornerradius=4),
                           text=[f"{v:.0f}" for v in s], textposition="outside",
                           textfont=dict(color=theme.muted, size=11),
                           hovertemplate="%{y}: %{x:.0f} of 100<extra></extra>"))
    fig.update_xaxes(range=[0, 108], showticklabels=False, showgrid=False)
    fig.update_yaxes(showgrid=False)
    return theme.apply(fig, height=height, legend=False)


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"
