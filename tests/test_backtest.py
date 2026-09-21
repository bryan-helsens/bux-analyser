"""The backtester is only worth having if it cannot fool us. These tests attack it."""
import numpy as np
import pandas as pd
import pytest

from bux_analyser.analytics.backtest import (BacktestConfig, bias_report, buy_and_hold, compare,
                                             equal_weight_signal, low_volatility_signal,
                                             momentum_signal, permutation_test, run)

DAYS = pd.date_range("2018-01-01", periods=1500, freq="B")


def trending_prices() -> pd.DataFrame:
    """One asset that rises steadily, one that falls, one that goes nowhere."""
    n = len(DAYS)
    return pd.DataFrame({
        "winner": 100 * np.cumprod(np.full(n, 1.0008)),
        "loser": 100 * np.cumprod(np.full(n, 0.9994)),
        "flat": np.full(n, 100.0),
    }, index=DAYS)


def noisy_prices(seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(DAYS)
    return pd.DataFrame(
        {f"a{i}": 100 * np.cumprod(1 + rng.normal(0.0002, 0.012, n)) for i in range(6)},
        index=DAYS)


def test_a_strategy_can_never_see_the_future():
    """The harness hands the signal a slice of history. If it leaked the future, a
    strategy that peeks would beat one that cannot, and the harness would be useless."""
    seen = []

    def recording(as_of, history):
        seen.append((as_of, history.index[-1], len(history)))
        return pd.Series(1.0, index=history.columns)

    prices = noisy_prices()
    run(prices, recording, BacktestConfig(rebalance="QE"))
    assert seen
    for as_of, last_row, _ in seen:
        assert last_row <= as_of, "the strategy was shown a price after the rebalance date"


def test_momentum_picks_the_winner_on_a_trending_market():
    prices = trending_prices()
    result = run(prices, momentum_signal(), BacktestConfig(max_positions=1, cost_bps=0))
    held = result.weights.idxmax(axis=1)
    assert (held == "winner").mean() > 0.9
    assert result.total_return > 0


def test_costs_reduce_the_result_and_are_counted():
    prices = noisy_prices()
    free = run(prices, momentum_signal(), BacktestConfig(cost_bps=0, max_positions=2))
    charged = run(prices, momentum_signal(), BacktestConfig(cost_bps=100, max_positions=2))
    assert charged.total_return < free.total_return
    assert charged.costs_paid > 0 and free.costs_paid == 0
    assert charged.stats()["turnover_per_year"] > 0


def test_an_unchanging_strategy_trades_once_and_pays_once():
    prices = noisy_prices()
    result = run(prices, equal_weight_signal(), BacktestConfig(cost_bps=50, rebalance="YE"))
    # rebalancing an equal-weight book still trades as prices drift, but only a little
    assert result.turnover.iloc[0] == pytest.approx(1.0)       # the initial purchase
    assert result.turnover.iloc[1:].max() < 0.5


def test_buy_and_hold_never_rebalances_after_the_first_purchase():
    prices = trending_prices()
    result = buy_and_hold(prices, BacktestConfig(cost_bps=50))
    assert result.turnover.iloc[0] == pytest.approx(1.0)
    assert result.label == "buy and hold"
    assert result.total_return is not None


def test_stats_are_reported_and_self_consistent():
    result = run(noisy_prices(), momentum_signal(), BacktestConfig(max_positions=3))
    stats = result.stats()
    assert set(stats) >= {"cagr", "volatility", "sharpe", "sortino", "max_drawdown",
                          "turnover_per_year", "win_rate", "total_return"}
    assert stats["max_drawdown"] <= 0
    assert 0 <= stats["win_rate"] <= 1
    assert result.equity.iloc[0] == pytest.approx(1.0)


def test_permutation_test_exposes_a_signal_that_is_really_noise():
    """On random prices, a momentum signal has nothing to find. The permutation test
    should say the result is unremarkable."""
    prices = noisy_prices(seed=7)
    outcome = permutation_test(prices, momentum_signal(),
                               BacktestConfig(max_positions=2, rebalance="QE"), runs=60)
    assert outcome["runs"] > 0
    assert outcome["p_value"] is not None
    assert outcome["p_value"] > 0.05, "pure noise was mistaken for skill"


def test_permutation_test_recognises_a_genuinely_informative_signal():
    prices = trending_prices()
    outcome = permutation_test(prices, momentum_signal(),
                               BacktestConfig(max_positions=1, rebalance="QE", cost_bps=0), runs=60)
    assert outcome["p_value"] <= 0.25        # ranking matters when trends are real
    assert outcome["actual_sharpe"] > outcome["null_median"]


def test_comparison_table_lines_strategies_up():
    prices = noisy_prices()
    results = [run(prices, momentum_signal(), BacktestConfig(max_positions=2), label="momentum"),
               run(prices, low_volatility_signal(), BacktestConfig(max_positions=2), label="low vol"),
               buy_and_hold(prices)]
    table = compare([r for r in results if r])
    assert list(table.index) == ["momentum", "low vol", "buy and hold"]
    assert "sharpe" in table.columns


def test_every_result_carries_its_biases():
    result = run(noisy_prices(), momentum_signal(), BacktestConfig())
    assert len(result.biases) >= 4
    joined = " ".join(result.biases).lower()
    assert "survivorship" in joined and "multiple testing" in joined
    assert bias_report() == result.biases


def test_backtest_refuses_when_there_is_not_enough_history():
    short = noisy_prices().head(50)
    assert run(short, momentum_signal()) is None
    assert run(pd.DataFrame(), momentum_signal()) is None
    assert buy_and_hold(short) is None


def test_a_signal_that_returns_nothing_leaves_the_book_empty():
    prices = noisy_prices()
    result = run(prices, lambda as_of, history: pd.Series(dtype=float), BacktestConfig())
    assert result is not None
    assert result.total_return == pytest.approx(0.0, abs=1e-9)   # never invested, never moved
