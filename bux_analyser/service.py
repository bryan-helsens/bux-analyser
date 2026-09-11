"""Application service: ties importer, ledger, market-data store and performance together.
Everything the dashboard shows comes from here; nothing here talks to a provider directly."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import BASE_CURRENCY
from .core.ledger import build_ledger
from .core.performance import portfolio_xirr, time_weighted_return, valuation_history
from .core.types import Ledger, Txn
from .db import PriceEod, QuoteCache, Security
from .importers.persist import load_txns
from .marketdata.base import Provenance
from .marketdata.ecb import ECBProvider
from .marketdata.store import MarketDataRouter, MarketDataStore
from .marketdata.yahoo import YahooProvider


def default_store(session: Session) -> MarketDataStore:
    return MarketDataStore(session, MarketDataRouter([YahooProvider()], [ECBProvider()]))


@dataclass
class Holding:
    isin: str
    name: str
    currency: str            # trading currency at the broker
    quantity: Decimal
    avg_cost_local: Decimal
    avg_cost_base: Decimal
    cost_base: Decimal
    price: float | None      # latest price in price_currency
    price_currency: str | None
    price_provenance: Provenance | None
    fx: float | None         # base per 1 price_currency
    fx_provenance: Provenance | None
    value_base: float | None
    unrealized_base: float | None
    unrealized_pct: float | None
    day_change_base: float | None
    day_change_pct: float | None
    dividends_base: Decimal
    fees_base: Decimal
    taxes_base: Decimal
    realized_pl_base: Decimal
    weight: float | None = None
    ticker: str | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    as_of: datetime
    ledger: Ledger
    holdings: list[Holding]
    history: pd.DataFrame
    twr: pd.Series
    total_value: float | None
    securities_value: float | None
    cash: float
    day_change: float | None
    day_change_pct: float | None
    unrealized: float | None
    mwr: float | None
    warnings: list[str]


def refresh_market_data(session: Session, store: MarketDataStore, txns: list[Txn] | None = None) -> list[str]:
    """Resolve symbols and refresh price/FX caches for every security ever held. Returns warnings."""
    txns = txns if txns is not None else load_txns(session)
    ledger = build_ledger(txns)
    for isin, p in ledger.positions.items():
        sec = session.get(Security, isin)
        if sec is not None and sec.asset_type == "crypto":
            continue  # no free crypto pricing; valued at cost in history (flagged)
        store.ensure_resolved(isin, p.name, p.currency)  # closed positions too: needed for the value history
    first = min((t.date for t in txns), default=date.today())
    currencies = set()
    for isin, p in ledger.positions.items():
        sec = session.get(Security, isin)
        if sec is None or not sec.ticker or sec.asset_type == "crypto":
            continue
        start = p.first_buy or first
        s, _ = store.price_history(isin, start)
        if p.is_open:
            store.quote(isin)
        row = session.execute(select(PriceEod.currency).where(PriceEod.isin == isin).limit(1)).scalar()
        if row:
            currencies.add(row)
    for ccy in currencies | {p.currency for p in ledger.positions.values() if p.currency}:
        if ccy and ccy != BASE_CURRENCY:
            store.fx_history(ccy, first)
    session.commit()
    return list(store.warnings)


def build_snapshot(session: Session, store: MarketDataStore, refresh: bool = False) -> Snapshot:
    txns = load_txns(session)
    warnings: list[str] = []
    if refresh:
        warnings += refresh_market_data(session, store, txns)
    ledger = build_ledger(txns)
    warnings += ledger.warnings
    first = min((t.date for t in txns), default=date.today())
    today = date.today()

    prices: dict[str, pd.Series] = {}
    price_ccy: dict[str, str] = {}
    fx: dict[str, pd.Series] = {}
    fx_prov: dict[str, Provenance | None] = {}
    for isin in ledger.positions:
        s, _ = store.price_history(isin, first, refresh=False)
        if not s.empty:
            prices[isin] = s
            price_ccy[isin] = session.execute(select(PriceEod.currency).where(PriceEod.isin == isin).limit(1)).scalar() or BASE_CURRENCY
    # securities without any price series (crypto, unresolved) are valued at cost in the history, flagged
    at_cost = {}
    for isin, p in ledger.positions.items():
        if isin not in prices:
            buys = [t for t in txns if t.isin == isin and t.type.value in ("buy", "transfer_in") and t.quantity]
            if buys:
                at_cost[isin] = float(sum(t.gross_local for t in buys) / sum(t.quantity for t in buys))
                price_ccy.setdefault(isin, buys[0].currency)
    for ccy in set(price_ccy.values()):
        if ccy != BASE_CURRENCY:
            fx[ccy], fx_prov[ccy] = store.fx_history(ccy, first, refresh=False)
    fx_prov[BASE_CURRENCY] = Provenance("identity", "identity", Provenance.now(), today, "calculated")

    holdings: list[Holding] = []
    for isin, p in sorted(ledger.open_positions.items(), key=lambda kv: kv[0]):
        sec = session.get(Security, isin)
        q: QuoteCache | None = session.get(QuoteCache, isin)
        s = prices.get(isin)
        issues: list[str] = []
        price = prov = None
        pccy = price_ccy.get(isin)
        if q is not None:
            price, pccy = float(q.price), q.currency
            prov = Provenance(q.provider, q.source, q.retrieved_at, q.retrieved_at.date(), "cached", q.note)
        elif s is not None and not s.empty:
            price = float(s.iloc[-1])
            row = session.execute(select(PriceEod).where(PriceEod.isin == isin).order_by(PriceEod.date.desc()).limit(1)).scalar()
            prov = Provenance(row.provider, row.source, row.retrieved_at, row.date, "cached", "last close")
        else:
            issues.append("no price available (symbol unresolved or provider unreachable)")
        rate = rprov = None
        if pccy == BASE_CURRENCY:
            rate, rprov = 1.0, fx_prov[BASE_CURRENCY]
        elif pccy and pccy in fx and not fx[pccy].empty:
            rate, rprov = float(fx[pccy].iloc[-1]), fx_prov.get(pccy)
        elif pccy:
            issues.append(f"no FX rate for {pccy}")
        value = unreal = unreal_pct = dchg = dchg_pct = None
        if price is not None and rate is not None:
            value = float(p.quantity) * price * rate
            unreal = value - float(p.cost_base)
            unreal_pct = unreal / float(p.cost_base) if p.cost_base else None
            prev = float(q.previous_close) if (q is not None and q.previous_close) else (float(s.iloc[-2]) if s is not None and len(s) > 1 else None)
            if prev:
                dchg = float(p.quantity) * (price - prev) * rate
                dchg_pct = price / prev - 1.0
        if sec is not None and sec.currency and pccy and sec.currency != pccy:
            issues.append(f"priced in {pccy}, traded in {sec.currency} (converted with ECB rate)")
        holdings.append(Holding(isin=isin, name=p.name or (sec.name if sec else isin), currency=p.currency,
                                quantity=p.quantity, avg_cost_local=p.avg_cost_local, avg_cost_base=p.avg_cost_base,
                                cost_base=p.cost_base, price=price, price_currency=pccy, price_provenance=prov,
                                fx=rate, fx_provenance=rprov, value_base=value, unrealized_base=unreal,
                                unrealized_pct=unreal_pct, day_change_base=dchg, day_change_pct=dchg_pct,
                                dividends_base=p.dividends_base, fees_base=p.fees_base, taxes_base=p.taxes_base,
                                realized_pl_base=p.realized_pl_base, ticker=(sec.ticker if sec else None), issues=issues))

    sec_value = sum(h.value_base for h in holdings if h.value_base is not None) if holdings else 0.0
    missing = [h for h in holdings if h.value_base is None]
    cash = float(ledger.cash_base)
    total = None if missing else sec_value + cash
    if total:
        for h in holdings:
            h.weight = (h.value_base / total) if h.value_base is not None else None
    dchgs = [h.day_change_base for h in holdings if h.day_change_base is not None]
    day_change = sum(dchgs) if dchgs and not missing else None
    day_change_pct = (day_change / (total - day_change)) if (day_change is not None and total) else None
    unreal_total = None if missing else sum(h.unrealized_base for h in holdings)
    if missing:
        warnings.append("Portfolio total is unavailable because " + ", ".join(h.name for h in missing) + " has no price.")

    history = valuation_history(txns, prices, fx, price_ccy, BASE_CURRENCY, end=today, at_cost=at_cost)
    for isin, (d0, d1) in history.attrs.get("at_cost", {}).items():
        nm = ledger.positions[isin].name or isin
        warnings.append(f"{nm}: no market data; valued at cost in the history from {d0} to {d1}.")
    twr = time_weighted_return(history["total"], history["external_flow"]) if not history.empty else pd.Series(dtype=float)
    return Snapshot(as_of=datetime.now(timezone.utc), ledger=ledger, holdings=holdings, history=history, twr=twr,
                    total_value=total, securities_value=(None if missing else sec_value), cash=cash,
                    day_change=day_change, day_change_pct=day_change_pct, unrealized=unreal_total,
                    mwr=portfolio_xirr(history), warnings=warnings)
