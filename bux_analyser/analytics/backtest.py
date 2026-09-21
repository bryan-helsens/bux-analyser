"""A walk-forward backtesting harness.

The point of this module is not to produce a good-looking curve. It is to make the
usual ways of fooling yourself hard:

- **Look-ahead is prevented structurally.** A strategy is a function that receives only
  the price history up to and including the rebalance date. It never sees the future
  because it is never handed the future.
- **Costs are charged on every trade**, including the initial purchase.
- **A random strategy is run alongside**, many times, so a result can be compared with
  what pure luck produces on the same data. A Sharpe ratio that a coin flip beats one
  time in four is not evidence.
- **Known biases are reported with the result**, not buried in a footnote.

What it cannot fix: the universe is whatever holdings you have today, so companies that
failed and left the portfolio are missing. That inflates returns, and the bias report
says so on every run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

from .risk import annualised_volatility, max_drawdown, sharpe_ratio, sortino_ratio

TRADING_DAYS = 252
SignalFunction = Callable[[pd.Timestamp, pd.DataFrame], pd.Series]


@dataclass
class BacktestConfig:
    rebalance: str = "ME"              # pandas offset: ME monthly, QE quarterly
    cost_bps: float = 25.0             # one-way cost in basis points of traded value
    max_positions: int | None = None   # keep only the best N by signal
    min_history_days: int = 252        # days of data required before the first trade
    weighting: str = "equal"           # equal | signal
    seed: int = 0


@dataclass
class BacktestResult:
    equity: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    costs_paid: float
    config: BacktestConfig
    label: str = "strategy"
    biases: list[str] = field(default_factory=list)

    @property
    def total_return(self) -> float | None:
        return float(self.equity.iloc[-1] - 1) if len(self.equity) else None

    def stats(self, risk_free: float = 0.0) -> dict[str, float | None]:
        r, e = self.returns, self.equity
        years = (e.index[-1] - e.index[0]).days / 365.25 if len(e) > 1 else 0
        dd = max_drawdown(e)
        return {
            "total_return": self.total_return,
            "cagr": (float(e.iloc[-1] ** (1 / years) - 1) if years > 0 and e.iloc[-1] > 0 else None),
            "volatility": annualised_volatility(r),
            "sharpe": sharpe_ratio(r, risk_free),
            "sortino": sortino_ratio(r, risk_free),
            "max_drawdown": dd.max_drawdown if dd else None,
            "turnover_per_year": (float(self.turnover.sum() / years) if years > 0 else None),
            "costs_paid": self.costs_paid,
            "win_rate": (float((r > 0).mean()) if len(r) else None),
        }


def _rebalance_dates(index: pd.DatetimeIndex, config: BacktestConfig) -> list[pd.Timestamp]:
    if len(index) == 0:
        return []
    start = index[0] + pd.Timedelta(days=config.min_history_days)
    usable = index[index >= start]
    if len(usable) == 0:
        return []
    marks = pd.Series(usable, index=usable).resample(config.rebalance).last().dropna()
    return list(pd.DatetimeIndex(marks.to_numpy()))


def _target_weights(signal: pd.Series, config: BacktestConfig) -> pd.Series:
    s = signal.dropna()
    s = s[np.isfinite(s.to_numpy())]
    if s.empty:
        return pd.Series(dtype=float)
    if config.max_positions:
        s = s.sort_values(ascending=False).head(config.max_positions)
    if config.weighting == "signal":
        positive = s.clip(lower=0)
        if positive.sum() > 0:
            return positive / positive.sum()
    return pd.Series(1.0 / len(s), index=s.index)


def run(prices: pd.DataFrame, signal: SignalFunction, config: BacktestConfig | None = None,
        label: str = "strategy") -> BacktestResult | None:
    """Walk forward through the price history, rebalancing on schedule.

    `signal` is called as `signal(as_of, history)` where `history` ends at `as_of`. It
    never receives a later row, so look-ahead is impossible rather than merely
    discouraged.

    The inner loop works on numpy arrays: a permutation test runs the whole backtest
    hundreds of times, and pandas indexing per day makes that unusably slow.
    """
    config = config or BacktestConfig()
    if prices is None or prices.empty or len(prices) < config.min_history_days + 2:
        return None
    px = prices.sort_index().astype(float)
    columns = list(px.columns)
    position_of = {c: i for i, c in enumerate(columns)}
    daily = px.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()
    index = px.index
    marks = _rebalance_dates(index, config)
    if not marks:
        return None
    mark_positions = sorted({index.get_indexer([m], method="nearest")[0] for m in marks})
    is_mark = np.zeros(len(index), dtype=bool)
    is_mark[mark_positions] = True

    n = len(columns)
    weights = np.zeros(n)
    value, costs_total = 1.0, 0.0
    cost_rate = config.cost_bps / 10_000.0
    start_position = mark_positions[0]
    equity = np.empty(len(index) - start_position)
    equity[0] = 1.0
    turnovers, weight_rows = [], {}

    for step, i in enumerate(range(start_position, len(index))):
        if step:
            day_returns = daily[i]
            growth = 1.0 + float(day_returns @ weights)
            value *= growth
            if weights.any():
                grown = weights * (1.0 + day_returns)
                total = grown.sum()
                weights = grown / total if total > 0 else grown
        if is_mark[i]:
            as_of = index[i]
            target_series = _target_weights(signal(as_of, px.iloc[:i + 1]), config)
            target = np.zeros(n)
            for name, w in target_series.items():
                pos = position_of.get(name)
                if pos is not None:
                    target[pos] = float(w)
            traded = float(np.abs(target - weights).sum())
            cost = traded * cost_rate
            value *= (1.0 - cost)
            costs_total += cost
            turnovers.append((as_of, traded))
            weights = target
            weight_rows[as_of] = target_series
        if step:
            equity[step] = value

    curve = pd.Series(equity, index=index[start_position:]).dropna()
    turnover_series = (pd.Series([t for _, t in turnovers],
                                 index=pd.DatetimeIndex([d for d, _ in turnovers]))
                       if turnovers else pd.Series(dtype=float))
    return BacktestResult(
        equity=curve, returns=curve.pct_change().dropna(),
        weights=pd.DataFrame(weight_rows).T.fillna(0.0) if weight_rows else pd.DataFrame(),
        turnover=turnover_series, costs_paid=costs_total, config=config, label=label,
        biases=bias_report())


def buy_and_hold(prices: pd.DataFrame, config: BacktestConfig | None = None,
                 label: str = "buy and hold") -> BacktestResult | None:
    """Equal amounts of everything at the start, then left alone.

    The benchmark any strategy has to beat, because it costs nothing to follow.
    """
    config = config or BacktestConfig()
    held = BacktestConfig(rebalance=config.rebalance, cost_bps=config.cost_bps,
                          max_positions=None, min_history_days=config.min_history_days,
                          weighting="equal", seed=config.seed)
    state: dict[str, pd.Series] = {}

    def constant(as_of, history):
        if "weights" not in state:
            state["weights"] = pd.Series(1.0, index=history.columns)
        return state["weights"]

    return run(prices, constant, held, label=label)


def permutation_test(prices: pd.DataFrame, signal: SignalFunction, config: BacktestConfig | None = None,
                     runs: int = 200) -> dict[str, float | None]:
    """Compare the strategy with the same strategy fed shuffled scores.

    If reordering the signal at random produces a similar Sharpe ratio, the signal
    carried no information and the result was the shape of the data, not skill.
    """
    config = config or BacktestConfig()
    actual = run(prices, signal, config)
    if actual is None:
        return {"p_value": None, "actual_sharpe": None, "null_median": None, "runs": 0}
    actual_sharpe = actual.stats().get("sharpe")
    if actual_sharpe is None:
        return {"p_value": None, "actual_sharpe": None, "null_median": None, "runs": 0}

    rng = np.random.default_rng(config.seed)
    nulls = []
    for _ in range(runs):
        def shuffled(as_of, history, _rng=rng):
            base = signal(as_of, history).dropna()
            if base.empty:
                return base
            return pd.Series(_rng.permutation(base.to_numpy()), index=base.index)
        candidate = run(prices, shuffled, config)
        if candidate is None:
            continue
        s = candidate.stats().get("sharpe")
        if s is not None:
            nulls.append(s)
    if not nulls:
        return {"p_value": None, "actual_sharpe": actual_sharpe, "null_median": None, "runs": 0}
    nulls_array = np.array(nulls)
    return {"p_value": float((nulls_array >= actual_sharpe).mean()),
            "actual_sharpe": float(actual_sharpe),
            "null_median": float(np.median(nulls_array)),
            "null_p95": float(np.percentile(nulls_array, 95)),
            "runs": len(nulls)}


def compare(results: list[BacktestResult], risk_free: float = 0.0) -> pd.DataFrame:
    rows = []
    for r in results:
        stats = r.stats(risk_free)
        stats["strategy"] = r.label
        rows.append(stats)
    frame = pd.DataFrame(rows).set_index("strategy")
    return frame


def bias_report() -> list[str]:
    """What this harness cannot correct for. Shown with every result."""
    return [
        "Survivorship: the universe is the holdings you own today, so anything you sold "
        "at a loss or that was delisted is missing. This flatters every result.",
        "Selection: those holdings were chosen by you, with hindsight about which ones "
        "were worth buying.",
        "Short history: a few years spans one market regime, not several.",
        "Costs: modelled as a flat spread-and-fee charge on traded value. Real fills, "
        "taxes and dividend withholding are not modelled.",
        "Multiple testing: every variant you try raises the chance that one looks good "
        "by luck. Compare against the permutation test, not against zero.",
    ]


# ---------------------------------------------------------------- ready-made signals
def momentum_signal(lookback_days: int = 252, skip_days: int = 21) -> SignalFunction:
    """Rank by past return, excluding the most recent month."""
    def signal(as_of: pd.Timestamp, history: pd.DataFrame) -> pd.Series:
        if len(history) < lookback_days + 1:
            return pd.Series(dtype=float)
        end = history.iloc[-1 - skip_days] if skip_days else history.iloc[-1]
        start = history.iloc[-1 - lookback_days]
        return (end / start - 1).replace([np.inf, -np.inf], np.nan)
    return signal


def low_volatility_signal(window: int = 126) -> SignalFunction:
    """Rank by the inverse of recent volatility."""
    def signal(as_of: pd.Timestamp, history: pd.DataFrame) -> pd.Series:
        if len(history) < window + 1:
            return pd.Series(dtype=float)
        vol = history.pct_change().tail(window).std()
        return (-vol).replace([np.inf, -np.inf], np.nan)
    return signal


def equal_weight_signal() -> SignalFunction:
    def signal(as_of: pd.Timestamp, history: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=history.columns)
    return signal
