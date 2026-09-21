"""Charts must build for real data, for empty data, and in both themes."""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pytest

from bux_analyser.analytics.portfolio import BenchmarkComparison
from bux_analyser.ui import charts
from bux_analyser.ui.theme import MAX_SERIES, Theme

IDX = pd.date_range("2024-01-01", periods=60)
THEMES = [Theme(dark=False), Theme(dark=True)]


@pytest.fixture(params=THEMES, ids=["light", "dark"])
def theme(request):
    return request.param


def test_fixed_order_colours_never_wrap_to_a_reused_hue(theme):
    assert theme.color(0) != theme.color(1)
    assert len({theme.color(i) for i in range(MAX_SERIES)}) == MAX_SERIES
    assert theme.color(MAX_SERIES) == theme.neutral      # folds to neutral, not back to slot 1


def test_diverging_scale_has_a_neutral_midpoint(theme):
    scale = theme.diverging
    assert scale[0][0] == 0.0 and scale[1][0] == 0.5 and scale[2][0] == 1.0
    assert scale[0][1] == theme.negative and scale[2][1] == theme.positive


def test_every_chart_builds_with_real_data(theme):
    hist = pd.DataFrame({"total": np.linspace(100, 130, 60),
                         "net_invested": np.full(60, 100.0)}, index=IDX)
    twr = pd.Series(np.linspace(1.0, 1.3, 60), index=IDX)
    bench = [BenchmarkComparison("MSCI World", pd.Series(np.linspace(1.0, 1.2, 60), index=IDX), 0.2, 0.3)]
    corr = pd.DataFrame([[1.0, 0.4], [0.4, 1.0]], index=["A", "B"], columns=["A", "B"])
    figs = [
        charts.value_over_time(hist, theme),
        charts.benchmark_comparison(twr, bench, theme),
        charts.horizontal_bars(pd.Series({"a": 0.5, "b": 0.3, "c": 0.2}), theme),
        charts.horizontal_bars(pd.Series({"a": 1200.0, "b": 400.0}), theme, money=True),
        charts.grouped_bars(pd.DataFrame({"weight": [0.5, 0.5], "risk": [0.8, 0.2]}, index=["A", "B"]), theme),
        charts.returns_heatmap(pd.DataFrame({"Jan": [0.01, 0.02], "Feb": [-0.02, np.nan]}, index=[2024, 2025])),
        charts.correlation_heatmap(corr, theme),
        charts.drawdown_area(twr, theme),
        charts.decomposition_bars(pd.DataFrame({"local": [0.3], "fx": [-0.05]}, index=["Acme"]), theme),
        charts.price_comparison({"A": pd.Series(np.linspace(10, 12, 60), index=IDX)}, theme),
    ]
    assert all(isinstance(f, go.Figure) and f.layout.height for f in figs)


def test_every_chart_explains_itself_when_there_is_no_data(theme):
    empty_df, empty_s = pd.DataFrame(), pd.Series(dtype=float)
    figs = [
        charts.value_over_time(empty_df, theme),
        charts.benchmark_comparison(empty_s, [], theme),
        charts.horizontal_bars(empty_s, theme),
        charts.grouped_bars(empty_df, theme),
        charts.returns_heatmap(empty_df, theme),
        charts.correlation_heatmap(empty_df, theme),
        charts.drawdown_area(empty_s, theme),
        charts.decomposition_bars(empty_df, theme),
        charts.price_comparison({}, theme),
    ]
    for f in figs:
        assert len(f.layout.annotations) == 1 and f.layout.annotations[0].text
        assert not f.data          # nothing plotted, a message instead of a blank frame


def test_long_tails_fold_into_other_rather_than_adding_hues(theme):
    many = pd.Series({f"h{i}": 1.0 / 20 for i in range(20)})
    fig = charts.horizontal_bars(many, theme, max_items=5)
    labels = list(fig.data[0].y)
    assert len(labels) == 5 and "Other" in labels
    assert sum(fig.data[0].x) == pytest.approx(1.0)


def test_benchmark_chart_caps_series_at_the_palette_size(theme):
    many = [BenchmarkComparison(f"B{i}", pd.Series(np.linspace(1, 1.1, 60), index=IDX), 0.1, 0.1)
            for i in range(12)]
    fig = charts.benchmark_comparison(pd.Series(np.linspace(1, 1.2, 60), index=IDX), many, theme)
    assert len(fig.data) <= MAX_SERIES


def test_returns_heatmap_centres_on_zero():
    fig = charts.returns_heatmap(pd.DataFrame({"Jan": [0.05], "Feb": [-0.01]}, index=[2024]))
    assert fig.data[0].zmid == 0
    assert fig.data[0].zmin == pytest.approx(-0.05) and fig.data[0].zmax == pytest.approx(0.05)


def test_simulation_charts_build_and_degrade(theme):
    paths = pd.DataFrame({"p5": [100, 90], "p25": [100, 95], "p50": [100, 105],
                          "p75": [100, 115], "p95": [100, 130]}, index=[0.0, 1.0])
    terminal = np.random.default_rng(0).normal(11000, 1500, 500)
    built = [charts.fan_chart(paths, 100.0, theme),
             charts.outcome_distribution(terminal, 10000, theme),
             charts.score_bars({"Momentum": 72.0, "Risk": 41.0}, theme)]
    assert all(isinstance(f, go.Figure) for f in built)
    empty = [charts.fan_chart(pd.DataFrame(), 0, theme),
             charts.outcome_distribution(np.array([]), 0, theme),
             charts.score_bars({}, theme)]
    for f in empty:
        assert len(f.layout.annotations) == 1 and not f.data


def test_fan_chart_draws_bands_and_a_median(theme):
    paths = pd.DataFrame({"p5": [100, 90], "p25": [100, 95], "p50": [100, 105],
                          "p75": [100, 115], "p95": [100, 130]}, index=[0.0, 1.0])
    fig = charts.fan_chart(paths, 100.0, theme, invested=pd.Series([100.0, 100.0], index=[0.0, 1.0]))
    names = [t.name for t in fig.data if t.name]
    assert "Median outcome" in names and "Money put in" in names
    assert any("95th percentile" in n for n in names)
