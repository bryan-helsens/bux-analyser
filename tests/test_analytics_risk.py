import numpy as np
import pandas as pd
import pytest

from bux_analyser.analytics.risk import (annualised_volatility, beta_alpha, capture_ratios,
                                         concentration, correlation_matrix, historical_var,
                                         max_drawdown, risk_contribution, sharpe_ratio,
                                         sortino_ratio, summarise, tracking_stats)

DAYS = pd.date_range("2024-01-01", periods=300, freq="B")


def alternating(magnitude: float, n: int = 300) -> pd.Series:
    """Deterministic zero-mean series: +m, -m, +m, ..."""
    return pd.Series(np.tile([magnitude, -magnitude], n // 2), index=DAYS[:n])


def blocks(magnitude: float, n: int = 300) -> pd.Series:
    """Deterministic zero-mean series orthogonal to `alternating`: ++, --, ++, ..."""
    return pd.Series(np.tile([magnitude, magnitude, -magnitude, -magnitude], n // 4), index=DAYS[:n])


def test_volatility_matches_the_closed_form():
    r = alternating(0.01)
    n = len(r)
    expected = 0.01 * np.sqrt(n / (n - 1)) * np.sqrt(252)   # sample std with ddof=1
    assert annualised_volatility(r) == pytest.approx(expected, rel=1e-9)


def test_statistics_refuse_to_report_on_too_few_observations():
    tiny = pd.Series([0.01, -0.01], index=DAYS[:2])
    assert annualised_volatility(tiny) is None
    assert sharpe_ratio(tiny) is None and sortino_ratio(tiny) is None
    assert historical_var(tiny) == (None, None)
    assert summarise(tiny).n_obs == 2 and not summarise(tiny).reliable


def test_sharpe_is_zero_for_a_zero_mean_series_and_sortino_exceeds_it_when_skewed():
    assert sharpe_ratio(alternating(0.01)) == pytest.approx(0.0, abs=1e-12)
    # small frequent losses, one large gain -> positive mean, limited downside
    r = pd.Series([-0.001] * 99 + [0.20], index=DAYS[:100])
    assert sortino_ratio(r) > sharpe_ratio(r) > 0


def test_max_drawdown_reports_depth_dates_and_recovery():
    lv = pd.Series([100.0, 120.0, 60.0, 90.0, 130.0], index=pd.date_range("2024-01-01", periods=5))
    d = max_drawdown(lv)
    assert d.max_drawdown == pytest.approx(-0.5)
    assert d.peak.isoformat() == "2024-01-02" and d.trough.isoformat() == "2024-01-03"
    assert d.recovered.isoformat() == "2024-01-05" and d.days_under_water == 3


def test_max_drawdown_marks_a_portfolio_still_under_water():
    lv = pd.Series([100.0, 50.0, 60.0], index=pd.date_range("2024-01-01", periods=3))
    d = max_drawdown(lv)
    assert d.recovered is None and d.max_drawdown == pytest.approx(-0.5)
    assert max_drawdown(pd.Series([1.0])) is None


def test_var_and_cvar_are_ordered_and_negative():
    r = pd.Series(np.linspace(-0.05, 0.05, 100), index=DAYS[:100])
    var, cvar = historical_var(r)
    assert cvar <= var < 0


def test_beta_of_a_doubled_benchmark_is_two_with_zero_alpha():
    b = blocks(0.01)
    p = b * 2
    beta, alpha = beta_alpha(p, b)
    assert beta == pytest.approx(2.0) and alpha == pytest.approx(0.0, abs=1e-9)


def test_tracking_error_is_zero_when_tracking_perfectly():
    b = blocks(0.01)
    te, ir = tracking_stats(b, b)
    assert te == pytest.approx(0.0, abs=1e-15) and ir is None   # no dispersion, ratio undefined


def test_capture_ratios_are_exactly_one_for_an_index_tracker():
    b = blocks(0.01)
    up, down = capture_ratios(b, b)
    assert up == pytest.approx(1.0) and down == pytest.approx(1.0)


def test_capture_ratios_split_up_and_down_markets():
    b = blocks(0.01)
    p = b.copy()
    p[b > 0] = 0.02          # more upside
    p[b < 0] = -0.005        # less downside
    up, down = capture_ratios(p, b)
    assert up > 1.0 > down > 0    # geometric, so the magnitude is not a plain multiple


def test_risk_contribution_sums_to_one_and_exceeds_weight_for_the_volatile_holding():
    returns = pd.DataFrame({"calm": alternating(0.01), "wild": blocks(0.02)})
    w = pd.Series({"calm": 0.5, "wild": 0.5})
    rc = risk_contribution(w, returns)
    assert rc["pct_of_risk"].sum() == pytest.approx(1.0)
    # equal weights, one holding twice as volatile and uncorrelated -> 20/80 split of risk
    assert rc.loc["wild", "pct_of_risk"] == pytest.approx(0.8, rel=0.02)
    assert rc.loc["calm", "pct_of_risk"] == pytest.approx(0.2, rel=0.02)
    assert rc.loc["wild", "pct_of_risk"] > rc.loc["wild", "weight"]


def test_risk_contribution_needs_at_least_two_usable_holdings():
    returns = pd.DataFrame({"a": alternating(0.01)})
    assert risk_contribution(pd.Series({"a": 1.0}), returns).empty
    assert risk_contribution(pd.Series(dtype=float), pd.DataFrame()).empty


def test_correlation_matrix_drops_series_with_too_little_history():
    returns = pd.DataFrame({"a": alternating(0.01), "b": alternating(0.01) * -1,
                            "short": pd.Series([0.01] * 5, index=DAYS[:5])})
    c = correlation_matrix(returns)
    assert list(c.columns) == ["a", "b"]
    assert c.loc["a", "b"] == pytest.approx(-1.0)


def test_concentration_measures_a_lopsided_portfolio():
    c = concentration(pd.Series({"big": 0.5, "mid": 0.3, "small": 0.2}))
    assert c.hhi == pytest.approx(0.38) and c.effective_holdings == pytest.approx(1 / 0.38)
    assert c.top1 == pytest.approx(0.5) and c.top3 == pytest.approx(1.0) and c.n == 3
    equal = concentration(pd.Series({f"h{i}": 0.1 for i in range(10)}))
    assert equal.effective_holdings == pytest.approx(10.0)
    assert concentration(pd.Series(dtype=float)) is None


def test_summarise_gathers_everything_and_flags_reliability():
    b = blocks(0.01)
    p = b * 1.5
    s = summarise(p, benchmark_returns=b, benchmark_name="MSCI World")
    assert s.n_obs == 300 and s.reliable
    assert s.beta == pytest.approx(1.5) and s.volatility > 0
    assert s.max_drawdown < 0 and s.drawdown is not None
    assert s.benchmark == "MSCI World"
