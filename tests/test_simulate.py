import numpy as np
import pandas as pd
import pytest

from bux_analyser.analytics.simulate import (Assumptions, _block_indices, equal_weights, group_shock,
                                             minimum_variance_weights, portfolio_returns,
                                             risk_parity_weights, simulate_portfolio,
                                             uniform_shock, worst_observed)

IDX = pd.date_range("2022-01-03", periods=750, freq="B")


def steady(daily=0.0004, vol=0.01, n=750, seed=0):
    rng = np.random.default_rng(seed)
    return pd.Series(daily + vol * rng.standard_normal(n), index=IDX[:n])


def test_zero_drift_keeps_the_median_near_the_starting_value():
    sim = simulate_portfolio(steady(), 10_000, horizon_years=1, paths=3000, drift="zero", seed=1)
    assert sim.median == pytest.approx(10_000, rel=0.05)
    assert sim.percentile(5) < sim.median < sim.percentile(95)
    assert 0.3 < sim.probability_of_loss < 0.7      # a coin flip when nothing is expected


def test_fixed_drift_is_honoured_and_recorded():
    sim = simulate_portfolio(steady(), 10_000, horizon_years=1, paths=4000,
                             drift="fixed", annual_drift=0.08, seed=2)
    assert sim.median == pytest.approx(10_800, rel=0.05)
    assert sim.assumptions.drift == "fixed" and sim.assumptions.annual_drift == 0.08
    assert "8.0% a year" in sim.assumptions.describe()


def test_historical_drift_is_flagged_as_a_weak_basis():
    sim = simulate_portfolio(steady(), 10_000, horizon_years=1, paths=500, drift="historical", seed=3)
    assert any("weak basis" in n for n in sim.notes)
    assert "own history" in sim.assumptions.describe()


def test_blocks_are_drawn_as_consecutive_days():
    idx = _block_indices(100, 12, 3, 4, np.random.default_rng(0))
    assert idx.shape == (3, 12)
    for row in idx:
        for block_start in range(0, 12, 4):
            block = row[block_start:block_start + 4]
            assert list(block) == list(range(block[0], block[0] + 4))


def test_block_bootstrap_preserves_streaks_that_single_days_would_erase():
    """Markets trend and panic in runs. Sampling whole blocks keeps those runs, so the
    range of outcomes stays honest; sampling single days averages them away."""
    runs = pd.Series(np.repeat(np.tile([0.01, -0.01], 10), 40)[:800],
                     index=pd.date_range("2022-01-03", periods=800, freq="B"))
    assert runs.autocorr(1) > 0.9                     # strongly persistent by construction
    spread = lambda s: s.percentile(95) - s.percentile(5)
    blocked = simulate_portfolio(runs, 100.0, horizon_years=1, paths=1500,
                                 block_days=40, drift="zero", seed=4)
    singles = simulate_portfolio(runs, 100.0, horizon_years=1, paths=1500,
                                 block_days=1, drift="zero", seed=4)
    assert spread(blocked) > spread(singles) * 2


def test_contributions_are_added_and_counted():
    sim = simulate_portfolio(steady(), 10_000, horizon_years=1, paths=1000, drift="zero",
                             monthly_contribution=300, seed=5)
    assert sim.contributed == pytest.approx(300 * 11, abs=300)   # 11 or 12 month boundaries
    assert sim.invested == pytest.approx(10_000 + sim.contributed)
    assert sim.median > 12_000
    assert "€300 a month" in sim.assumptions.describe()


def test_percentile_paths_widen_over_time():
    sim = simulate_portfolio(steady(), 10_000, horizon_years=3, paths=1500, drift="zero", seed=6)
    p = sim.percentile_paths
    first = p["p95"].iloc[0] - p["p5"].iloc[0]
    last = p["p95"].iloc[-1] - p["p5"].iloc[-1]
    assert last > first * 2
    assert list(p.columns) == ["p5", "p25", "p50", "p75", "p95"]
    assert p.index[-1] == pytest.approx(3.0, abs=0.05)


def test_summary_table_reports_returns_against_money_in():
    sim = simulate_portfolio(steady(), 10_000, horizon_years=1, paths=1000, drift="zero", seed=7)
    s = sim.summary()
    assert len(s) == 5 and set(s.columns) == {"Outcome", "Value", "Total return"}
    assert s["Value"].is_monotonic_increasing


def test_simulation_refuses_when_there_is_too_little_history():
    assert simulate_portfolio(pd.Series([0.01] * 10), 1000) is None
    assert simulate_portfolio(steady(), 0) is None
    assert simulate_portfolio(steady(), 1000, drift="fixed", annual_drift=None) is None


def test_short_history_is_flagged():
    sim = simulate_portfolio(steady(n=100), 10_000, horizon_years=1, paths=300, drift="zero", seed=8)
    assert any("too narrow" in n for n in sim.notes)


# ---------------------------------------------------------------- allocations
def test_portfolio_returns_apply_and_renormalise_weights():
    r = pd.DataFrame({"a": [0.10, 0.00], "b": [0.00, 0.20]})
    out = portfolio_returns(r, pd.Series({"a": 1.0, "b": 1.0}))
    assert list(out) == [0.05, 0.10]
    # weights that do not sum to one are normalised, unknown holdings ignored
    out2 = portfolio_returns(r, pd.Series({"a": 3.0, "b": 1.0, "ghost": 5.0}))
    assert out2.iloc[0] == pytest.approx(0.075)
    assert portfolio_returns(r, pd.Series({"ghost": 1.0})).empty


def test_minimum_variance_favours_the_calmer_holding():
    cov = pd.DataFrame([[0.04, 0.0], [0.0, 0.16]], index=["calm", "wild"], columns=["calm", "wild"])
    w = minimum_variance_weights(cov)
    assert w.sum() == pytest.approx(1.0) and (w >= 0).all()
    assert w["calm"] == pytest.approx(0.8, abs=0.02)      # inverse-variance solution


def test_risk_parity_equalises_risk_contributions():
    cov = pd.DataFrame([[0.04, 0.01], [0.01, 0.16]], index=["a", "b"], columns=["a", "b"])
    w = risk_parity_weights(cov)
    S = cov.to_numpy()
    wv = w.to_numpy()
    rc = wv * (S @ wv)
    assert w.sum() == pytest.approx(1.0)
    assert rc[0] == pytest.approx(rc[1], rel=1e-4)        # equal risk contribution, by definition
    assert w["a"] > w["b"]                                 # more of the calmer asset


def test_allocation_helpers_handle_degenerate_input():
    assert equal_weights([]).empty
    assert minimum_variance_weights(pd.DataFrame()).empty
    assert risk_parity_weights(pd.DataFrame()).empty


# ---------------------------------------------------------------- stress
def test_uniform_shock_passes_through_beta():
    w = pd.Series({"defensive": 0.5, "racy": 0.5})
    betas = pd.Series({"defensive": 0.5, "racy": 1.5})
    s = uniform_shock(w, betas, -0.30)
    assert s.portfolio_change == pytest.approx(-0.30)      # average beta is 1.0
    assert s.per_holding["racy"] == pytest.approx(-0.225)
    assert "beta" in s.basis and "Market -30%" == s.label


def test_uniform_shock_assumes_beta_one_when_unknown():
    s = uniform_shock(pd.Series({"x": 1.0}), pd.Series(dtype=float), -0.20)
    assert s.portfolio_change == pytest.approx(-0.20)


def test_group_shock_only_touches_the_named_bucket():
    w = pd.Series({"chip": 0.3, "bank": 0.7})
    sectors = pd.Series({"chip": "Technology", "bank": "Financials"})
    s = group_shock(w, sectors, "Technology", -0.40)
    assert s.portfolio_change == pytest.approx(-0.12)
    assert list(s.per_holding.index) == ["chip"]


def test_worst_observed_finds_the_real_bad_patch():
    r = pd.Series([0.001] * 100, index=IDX[:100])
    r.iloc[50] = -0.15
    w = worst_observed(r)
    assert set(w["Window"]) >= {"1 day", "1 week"}
    assert w.loc[w["Window"] == "1 day", "Worst"].iloc[0] == pytest.approx(-0.15)
    assert worst_observed(pd.Series(dtype=float)).empty
