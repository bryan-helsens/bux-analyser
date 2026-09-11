"""Position and cash accounting from normalised transactions (average-cost method,
matching what BUX displays). Pure, deterministic, Decimal throughout."""
from __future__ import annotations

from decimal import Decimal

from .types import D0, Ledger, Position, Txn, TxnType


def build_ledger(txns: list[Txn]) -> Ledger:
    positions: dict[str, Position] = {}
    cash = deposits = withdrawals = fees = taxes = interest = income = div_net = realized = other = D0
    warnings: list[str] = []

    trade_types = (TxnType.BUY, TxnType.SELL, TxnType.TRANSFER_IN, TxnType.TRANSFER_OUT)

    def pos_for(t: Txn) -> Position:
        p = positions.get(t.isin)
        if p is None:
            p = positions[t.isin] = Position(isin=t.isin, name=t.name, currency=t.currency if t.type in trade_types else "")
        if t.type in trade_types and not p.currency:
            p.currency = t.currency  # trading currency comes from trades, never from fee/tax/dividend rows
        if t.name and not p.name:
            p.name = t.name
        p.n_txns += 1
        p.last_txn = t.date
        return p

    for t in sorted(txns, key=lambda x: (x.date, x.id)):
        cash += t.cash_effect_base
        amt_b = (t.amount or D0) * t.fx_rate

        if t.type == TxnType.DEPOSIT:
            deposits += amt_b
        elif t.type == TxnType.WITHDRAWAL:
            withdrawals += -amt_b
        elif t.type == TxnType.INTEREST:
            interest += amt_b
        elif t.type == TxnType.INCOME:
            income += amt_b
        elif t.type == TxnType.FEE:
            fees += -amt_b
            if t.isin:
                pos_for(t).fees_base += -amt_b
        elif t.type == TxnType.TAX:
            taxes += -amt_b
            if t.isin:
                pos_for(t).taxes_base += -amt_b
        elif t.type == TxnType.OTHER:
            other += amt_b
            warnings.append(f"{t.date} unclassified cash row {t.id[:8]}: {amt_b:+.2f} {t.note}")
        elif not t.isin:
            warnings.append(f"{t.date} {t.type.value} row {t.id[:8]} has no ISIN; skipped")
        else:
            p = pos_for(t)
            fee_b, tax_b = t.fee * t.fx_rate, t.tax * t.fx_rate
            p.fees_base += fee_b
            fees += fee_b
            if t.type in (TxnType.BUY, TxnType.TRANSFER_IN):
                if p.first_buy is None:
                    p.first_buy = t.date
                p.quantity += t.quantity
                p.cost_local += t.gross_local
                p.cost_base += t.gross_local * t.fx_rate
                p.taxes_base += tax_b
                taxes += tax_b
            elif t.type in (TxnType.SELL, TxnType.TRANSFER_OUT):
                if p.quantity <= 0:
                    p.warnings.append(f"{t.date} {t.type.value} with no holding; ignored")
                    warnings.append(f"{t.date} {t.type.value} of {t.isin} with no holding; ignored")
                    continue
                qty = t.quantity
                if qty > p.quantity:
                    warnings.append(f"{t.date} {t.type.value} of {qty} {t.isin} exceeds held {p.quantity}; clamped")
                    qty = p.quantity
                avg_l, avg_b = p.avg_cost_local, p.avg_cost_base
                if t.type == TxnType.SELL:
                    pl_l = t.gross_local - avg_l * qty
                    pl_b = t.gross_local * t.fx_rate - avg_b * qty
                    p.realized_pl_local += pl_l
                    p.realized_pl_base += pl_b
                    realized += pl_b
                    p.taxes_base += tax_b
                    taxes += tax_b
                p.quantity -= qty
                p.cost_local -= avg_l * qty
                p.cost_base -= avg_b * qty
                if p.quantity == 0:
                    p.cost_local = p.cost_base = D0
                if t.broker_pl is not None:
                    p.broker_realized_pl_base = (p.broker_realized_pl_base or D0) + t.broker_pl
            elif t.type == TxnType.DIVIDEND:
                gross_b = t.gross_local * t.fx_rate
                net_b = (t.gross_local - t.tax - t.fee) * t.fx_rate
                p.dividends_gross_base += gross_b
                p.dividend_tax_base += tax_b
                p.dividends_base += net_b
                div_net += net_b
                taxes += tax_b

    return Ledger(positions=positions, cash_base=cash, deposits_base=deposits,
                  withdrawals_base=withdrawals, fees_base=fees, taxes_base=taxes,
                  interest_base=interest, income_base=income, dividends_net_base=div_net,
                  realized_pl_base=realized, other_cash_base=other, warnings=warnings)


def holdings_on(txns: list[Txn], as_of) -> dict[str, Decimal]:
    """Quantity held per ISIN at end of `as_of` (inclusive)."""
    q: dict[str, Decimal] = {}
    for t in txns:
        if t.date > as_of or not t.isin:
            continue
        if t.type in (TxnType.BUY, TxnType.TRANSFER_IN):
            q[t.isin] = q.get(t.isin, D0) + t.quantity
        elif t.type in (TxnType.SELL, TxnType.TRANSFER_OUT):
            q[t.isin] = max(D0, q.get(t.isin, D0) - t.quantity)
    return {k: v for k, v in q.items() if v != 0}
