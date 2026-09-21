"""Technical indicators, used for the momentum score and for alerts.

These describe what a price has done. They are not predictions, and nothing in this
project treats a crossover or an RSI reading as a reason to trade.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def sma(prices: pd.Series, window: int) -> pd.Series:
    return prices.astype(float).rolling(window, min_periods=max(2, window // 2)).mean()


def rsi(prices: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's relative strength index."""
    p = prices.astype(float).dropna()
    if len(p) < window + 1:
        return pd.Series(dtype=float)
    delta = p.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(loss > 0, 100.0)


def macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    p = prices.astype(float)
    line = p.ewm(span=fast, adjust=False).mean() - p.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "histogram": line - sig})


def momentum(prices: pd.Series, months: int = 12, skip_months: int = 1) -> float | None:
    """The academic 12-1 momentum measure: return over the past year, excluding the
    most recent month, because the latest month tends to reverse."""
    p = prices.astype(float).dropna()
    days, skip = int(months * 21), int(skip_months * 21)
    if len(p) < days + 1:
        return None
    end = p.iloc[-1 - skip] if skip else p.iloc[-1]
    start = p.iloc[-1 - days]
    return float(end / start - 1) if start else None


def trailing_return(prices: pd.Series, days: int) -> float | None:
    p = prices.astype(float).dropna()
    if len(p) < days + 1:
        return None
    start = p.iloc[-1 - days]
    return float(p.iloc[-1] / start - 1) if start else None


def distance_from_high(prices: pd.Series, days: int = TRADING_DAYS) -> float | None:
    """How far below its rolling high the price sits. Zero means at the high."""
    p = prices.astype(float).dropna().iloc[-days:]
    if len(p) < 2:
        return None
    high = float(p.max())
    return float(p.iloc[-1] / high - 1) if high else None


def above_average(prices: pd.Series, window: int = 200) -> float | None:
    """Price relative to its moving average, as a fraction."""
    p = prices.astype(float).dropna()
    if len(p) < max(2, window // 2):
        return None
    avg = sma(p, window).iloc[-1]
    return float(p.iloc[-1] / avg - 1) if avg and not np.isnan(avg) else None


def crossed(fast: pd.Series, slow: pd.Series) -> str | None:
    """'golden' when the fast average crossed above the slow one on the last bar,
    'death' when it crossed below, otherwise None."""
    f, s = fast.dropna(), slow.dropna()
    idx = f.index.intersection(s.index)
    if len(idx) < 2:
        return None
    f, s = f.loc[idx], s.loc[idx]
    before, after = f.iloc[-2] - s.iloc[-2], f.iloc[-1] - s.iloc[-1]
    if before <= 0 < after:
        return "golden"
    if before >= 0 > after:
        return "death"
    return None
