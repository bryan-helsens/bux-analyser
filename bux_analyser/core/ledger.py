"""Position and cash accounting from a list of normalised transactions.

Method: average cost (what BUX displays). Every number is Decimal. The function is
pure and order-independent (it sorts by date, then by id for determinism).
"""
from __future__ import annotations

from decimal import Decimal

from .types import D0, Ledger, Position, Txn, TxnType


def build_ledger(txns: list[Txn]) -> Ledger:
    positions: dict[str, Position] = {}
    cash = deposits = withdrawals = fees = taxes = interest = div_net = realized = other = D0
    warnings: list[str] = []

    for t in sorted(txns, key=lambda x: (x.date, x.id)):
        cash += t.cash_effect_base
        fees += (t.fee * t.fx_rate)
        taxes += (t.tax * t.fx_rate)

        if t.type == TxnType.DEPOSIT:
            deposits += (t.amount or D0) * t.fx_rate
            continue
        if t.type == TxnType.WITHDRAWAL:
            withdrawals += abs(t.amount or D0) * t.fx_rate
            continue
        if t.type == TxnType.INTEREST:
            interest += (t.amount or D0) * t.fx_rate
            continue
        if t.type == TxnType.FEE:
            fees += t.gross_local * t.fx_rate
            continue
        if t.type == TxnType.TAX:
            taxes += t.gross_local * t.fx_rate
            continue
        if t.type == TxnType.OTHER:
            other += t.cash_effect_base
            warnings.append(f"{t.date} OTHER cash row {t.id}: {t.cash_effect_base:+} {t.note}")
            continue

        # ---- security rows ----
        if not t.isin:
            warnings.append(f"{t.date} {t.type.value} row {t.id} has no ISIN; skipped")
            continue
        p = positions.get(t.isin)
        if p is None:
            p = positions[t.isin] = Position(isin=t.isin, name=t.name, currency=t.currency)
        if t.name and not p.name:
            p.name = t.name
        p.n_txns += 1
        p.last_txn = t.date
        p.fees_base += t.fee * t.fx_rate

        if t.type == TxnType.BUY:
            if p.first_buy is None:
                p.first_buy = t.date
            p.quantity += t.quantity
            p.cost_local += t.gross_local + t.fee + t.tax
            p.cost_base += (t.gross_local + t.fee + t.tax) * t.fx_rate

        elif t.type == TxnType.SELL:
            if t.quantity > p.quantity:
                warnings.append(
                    f"{t.date} sell of {t.quantity} {t.isin} exceeds held {p.quantity}; clamped"
                )
                qty = p.quantity
            else:
                qty = t.quantity
            if p.quantity == 0:
                p.warnings.append(f"{t.date} sell with zero holding; ignored")
                continue
            avg_l, avg_b = p.avg_cost_local, p.avg_cost_base
            proceeds_local = t.gross_local - t.fee - t.tax
            proceeds_base = proceeds_local * t.fx_rate
            p.realized_pl_local += proceeds_local - avg_l * qty
            p.realized_pl_base += proceeds_base - avg_b * qty
            realized += proceeds_base - avg_b * qty
            p.quantity -= qty
            p.cost_local -= avg_l * qty
            p.cost_base -= avg_b * qty
            if p.quantity == 0:  # avoid Decimal dust
                p.cost_local = p.cost_base = D0

        elif t.type == TxnType.DIVIDEND:
            gross_b = t.gross_local * t.fx_rate
            tax_b = t.tax * t.fx_rate
            net_b = gross_b - tax_b - t.fee * t.fx_rate
            p.dividends_gross_base += gross_b
            p.dividend_tax_base += tax_b
            p.dividends_base += net_b
            div_net += net_b

    return Ledger(
        positions=positions, cash_base=cash, deposits_base=deposits,
        withdrawals_base=withdrawals, fees_base=fees, taxes_base=taxes,
        interest_base=interest, dividends_net_base=div_net, realized_pl_base=realized,
        other_cash_base=other, warnings=warnings,
    )


def holdings_on(txns: list[Txn], as_of) -> dict[str, Decimal]:
    """Quantity held per ISIN at end of `as_of` (inclusive). Cheap helper for valuation."""
    q: dict[str, Decimal] = {}
    for t in txns:
        if t.date > as_of or not t.isin:
            continue
        if t.type == TxnType.BUY:
            q[t.isin] = q.get(t.isin, D0) + t.quantity
        elif t.type == TxnType.SELL:
            q[t.isin] = q.get(t.isin, D0) - t.quantity
    return {k: v for k, v in q.items() if v != 0}
