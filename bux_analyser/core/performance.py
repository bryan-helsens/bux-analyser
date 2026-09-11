"""Portfolio valuation history, time-weighted return and money-weighted return (XIRR).

Inputs are plain pandas objects so the engine can be tested without a database.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

from .types import EXTERNAL_FLOW_TYPES, Txn, TxnType


def _fx_frame(fx: dict[str, pd.Series], idx: pd.DatetimeIndex, base: str) -> pd.DataFrame:
    """Columns = currency, values = base per 1 unit, forward-filled onto idx."""
    cols = {base: pd.Series(1.0, index=idx)}
    for ccy, s in fx.items():
        cols[ccy] = s.sort_index().reindex(idx, method="ffill") if not s.empty else pd.Series(np.nan, index=idx)
    return pd.DataFrame(cols)


def valuation_history(
    txns: list[Txn],
    prices: dict[str, pd.Series],
    fx: dict[str, pd.Series],
    currency_of: dict[str, str],
    base: str = "EUR",
    end: date | None = None,
    at_cost: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Daily frame with columns: securities_value, cash, total, external_flow, net_invested.

    - prices[isin]: Series of local-currency closes indexed by Timestamp (gaps forward-filled).
    - fx[ccy]: Series of base-per-unit rates.
    - Cash is tracked in base currency from transaction cash effects (BUX cash is EUR).
    - external_flow: deposits (+) / withdrawals (−) on that day, in base.
    - at_cost[isin]: local-currency cost per unit to use when no price series exists
      (e.g. delisted or crypto). Days valued this way are listed in df.attrs["at_cost"].
      Without a fallback, days on which an unpriced security is held have total = NaN.
    """
    if not txns:
        return pd.DataFrame(columns=["securities_value", "cash", "total", "external_flow", "net_invested"])
    txns = sorted(txns, key=lambda t: (t.date, t.id))
    start = pd.Timestamp(txns[0].date)
    end_ts = pd.Timestamp(end or date.today())
    idx = pd.date_range(start, end_ts, freq="D")
    fxf = _fx_frame(fx, idx, base)

    # daily quantity per ISIN (cumulative), cash, flows
    qty = pd.DataFrame(0.0, index=idx, columns=sorted({t.isin for t in txns if t.isin}))
    cash = pd.Series(0.0, index=idx)
    flow = pd.Series(0.0, index=idx)
    for t in txns:
        d = pd.Timestamp(t.date)
        cash[d] += float(t.cash_effect_base)
        if t.type in EXTERNAL_FLOW_TYPES:
            flow[d] += float((t.amount or Decimal(0)) * t.fx_rate)
        if t.type in (TxnType.BUY, TxnType.TRANSFER_IN):
            qty.loc[d, t.isin] += float(t.quantity)
        elif t.type in (TxnType.SELL, TxnType.TRANSFER_OUT):
            qty.loc[d, t.isin] -= float(t.quantity)
    qty = qty.cumsum()
    cash = cash.cumsum()

    sec_val = pd.Series(0.0, index=idx)
    at_cost_used: dict[str, tuple[str, str]] = {}
    for isin in qty.columns:
        held = qty[isin]
        is_held = held.abs() > 1e-12
        if not is_held.any():
            continue
        p = prices.get(isin)
        if p is None or p.empty:
            if at_cost and isin in at_cost:
                p = pd.Series(float(at_cost[isin]), index=idx)
                days = idx[is_held]
                at_cost_used[isin] = (str(days[0].date()), str(days[-1].date()))
            else:
                sec_val = sec_val.where(~is_held, np.nan)  # unknown only on days it was held
                continue
        p = p.sort_index().reindex(idx, method="ffill")
        ccy = currency_of.get(isin, base)
        rate = fxf[ccy] if ccy in fxf.columns else pd.Series(np.nan, index=idx)  # unknown FX → unknown value
        sec_val += (held * p * rate).where(is_held, 0.0)

    out = pd.DataFrame({"securities_value": sec_val, "cash": cash, "external_flow": flow})
    out["total"] = out["securities_value"] + out["cash"]
    out["net_invested"] = flow.cumsum()
    out.attrs["at_cost"] = at_cost_used
    return out


def time_weighted_return(total: pd.Series, external_flow: pd.Series) -> pd.Series:
    """Daily-linked TWR index (starts at 1.0). Flows are assumed at start of day:
    r_t = (V_t − F_t) / V_{t−1} − 1. Days with V_{t−1} == 0 contribute r = 0."""
    v_prev = total.shift(1)
    r = (total - external_flow) / v_prev - 1.0
    r = r.where(v_prev > 0, 0.0).fillna(0.0)
    return (1.0 + r).cumprod()


def xirr(cashflows: list[tuple[date, float]], tol: float = 1e-9, max_iter: int = 200) -> float | None:
    """Annualised money-weighted return. Convention: money the investor puts in is
    negative, money taken out (and the terminal value) positive. Returns None when
    no sign change or no convergence. Bisection on [-0.9999, 10] then Newton polish."""
    if len(cashflows) < 2:
        return None
    cfs = sorted(cashflows)
    t0 = cfs[0][0]
    amts = np.array([a for _, a in cfs], dtype=float)
    if not ((amts > 0).any() and (amts < 0).any()):
        return None
    yrs = np.array([(d - t0).days / 365.0 for d, _ in cfs])

    def npv(r: float) -> float:
        return float(np.sum(amts / (1.0 + r) ** yrs))

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    while np.sign(f_lo) == np.sign(f_hi) and hi < 1e9:  # very short horizons annualise to huge rates
        hi *= 10
        f_hi = npv(hi)
    if np.sign(f_lo) == np.sign(f_hi):
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if np.sign(f_mid) == np.sign(f_lo):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return (lo + hi) / 2


MIN_XIRR_DAYS = 30


def portfolio_xirr(history: pd.DataFrame) -> float | None:
    """XIRR from the valuation history: external flows (sign-flipped) + terminal value.
    Returns None for histories shorter than MIN_XIRR_DAYS: annualising a few days of
    returns produces meaningless numbers, and the UI should say 'not yet available'."""
    if history.empty or (history.index[-1] - history.index[0]).days < MIN_XIRR_DAYS:
        return None
    flows = [(d.date(), -float(f)) for d, f in history["external_flow"].items() if f != 0]
    last = history.index[-1]
    terminal = history["total"].iloc[-1]
    if pd.isna(terminal):
        return None
    flows.append((last.date(), float(terminal)))
    return xirr(flows)
