"""Chart palette and Plotly defaults.

Colours come from a validated categorical set: hues are assigned in fixed order and
never cycled, so a holding keeps its colour when a filter changes the series count.
Sequential scales use one hue; diverging scales use two hues with a neutral grey
midpoint, which stays readable for colour-blind viewers where red/green does not.
"""
from __future__ import annotations

import plotly.graph_objects as go

SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
MAX_SERIES = 8          # beyond this, fold into "Other" rather than inventing hues
ALL_PAIRS_CAP = 3       # scatter-style charts where every pair must be separable


class Theme:
    def __init__(self, dark: bool = False):
        self.dark = dark
        self.series = SERIES_DARK if dark else SERIES_LIGHT
        self.text = "#ffffff" if dark else "#0b0b0b"
        self.muted = "#c3c2b7" if dark else "#52514e"
        self.grid = "rgba(255,255,255,0.10)" if dark else "rgba(0,0,0,0.08)"
        self.surface = "#1a1a19" if dark else "#fcfcfb"
        self.positive = self.series[0]          # blue: gains
        self.negative = "#e66767" if dark else "#e34948"
        self.neutral = "#6b6a66"

    def color(self, i: int) -> str:
        """Fixed-order assignment. Index beyond the palette returns a neutral rather
        than wrapping round to a hue already in use."""
        return self.series[i] if i < len(self.series) else self.neutral

    @property
    def diverging(self) -> list[list]:
        """Two hues through a neutral grey. Used for returns and correlations."""
        mid = "#3a3a38" if self.dark else "#ebebe8"
        return [[0.0, self.negative], [0.5, mid], [1.0, self.positive]]

    @property
    def sequential(self) -> list[list]:
        """One hue, light to dark, for pure magnitude."""
        return ([[0.0, "#12314f"], [1.0, "#7fb4f0"]] if self.dark
                else [[0.0, "#d6e6fa"], [1.0, "#1b4f8f"]])

    def apply(self, fig: go.Figure, height: int = 360, legend: bool = True, **kwargs) -> go.Figure:
        """Recessive axes, transparent surface so the page theme shows through."""
        fig.update_layout(
            height=height, margin=dict(t=30, b=30, l=10, r=10),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=self.text, size=12),
            hoverlabel=dict(font_size=12),
            showlegend=legend,
            legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0,
                        bgcolor="rgba(0,0,0,0)", font=dict(color=self.muted)),
            **kwargs)
        fig.update_xaxes(showgrid=False, zeroline=False, linecolor=self.grid,
                         tickfont=dict(color=self.muted))
        fig.update_yaxes(showgrid=True, gridcolor=self.grid, zeroline=False,
                         linecolor="rgba(0,0,0,0)", tickfont=dict(color=self.muted))
        return fig


def detect_theme() -> Theme:
    """Follow the viewer's Streamlit theme so charts match the page in both modes."""
    try:
        import streamlit as st
        base = getattr(getattr(st, "context", None), "theme", None)
        kind = getattr(base, "type", None) or st.get_option("theme.base")
        return Theme(dark=(str(kind).lower() == "dark"))
    except Exception:
        return Theme(dark=False)
