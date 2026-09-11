"""Pure, database-independent transaction and position types used by the engine.

The importer turns a broker file into a list of `Txn`; the engine never sees
broker-specific fields. Money is Decimal, never float.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


class TxnType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    TRANSFER_IN = "transfer_in"    # shares arrive without cash (at stated value)
    TRANSFER_OUT = "transfer_out"  # shares leave without cash (at average cost)
    FEE = "fee"                    # broker fee, trade-linked or account-level
    TAX = "tax"                    # transaction tax (TOB, FTT); refunds positive
    INTEREST = "interest"
    INCOME = "income"              # other income: securities lending, promotions
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    OTHER = "other"                # unclassified cash movement; always warned


EXTERNAL_FLOW_TYPES = {TxnType.DEPOSIT, TxnType.WITHDRAWAL}
CASH_ONLY_TYPES = {TxnType.FEE, TxnType.TAX, TxnType.INTEREST, TxnType.INCOME,
                   TxnType.DEPOSIT, TxnType.WITHDRAWAL, TxnType.OTHER}
D0 = Decimal("0")


@dataclass(frozen=True)
class Txn:
    """One normalised transaction.

    Conventions:
    - `currency` is the currency of `price`, `amount`, `fee`, `tax`.
    - `fx_rate` converts `currency` -> base: base_amount = local_amount * fx_rate (1 for base).
    - BUY/SELL/TRANSFER_*: `quantity` > 0, `price` >= 0; gross value = quantity * price
      unless `amount` is given (then |amount| is the gross value, e.g. when the broker
      reports a rounded price but an exact total).
    - DIVIDEND: `amount` = gross dividend (signed; reversal negative), `tax` = withholding
      (signed the same way), `fee` >= 0.
    - Cash-only types: `amount` = signed cash effect (deposit +, withdrawal −, fee −,
      tax charge −, tax refund +, interest/income +).
    - `fee`/`tax` on BUY/SELL rows are costs charged with the trade (>= 0). They are NOT
      part of the cost basis (BUX's average price excludes them); they are tracked separately.
    - `id` is stable (hash of the source row) so re-imports are idempotent.
    - `broker_pl` is the broker's own realized P/L for the row in base currency, if reported
      (used only for reconciliation).
    """
    id: str
    type: TxnType
    date: date
    currency: str
    fx_rate: Decimal = Decimal("1")
    isin: str | None = None
    name: str | None = None
    quantity: Decimal = D0
    price: Decimal = D0
    fee: Decimal = D0
    tax: Decimal = D0
    amount: Decimal | None = None
    note: str = ""
    broker_pl: Decimal | None = None

    @property
    def gross_local(self) -> Decimal:
        """Gross trade value (unsigned) or gross dividend (signed) in local currency."""
        if self.type == TxnType.DIVIDEND:
            return self.amount or D0
        if self.type in (TxnType.BUY, TxnType.SELL, TxnType.TRANSFER_IN, TxnType.TRANSFER_OUT):
            return abs(self.amount) if self.amount is not None else self.quantity * self.price
        return abs(self.amount or D0)

    @property
    def cash_effect_local(self) -> Decimal:
        t = self.type
        if t == TxnType.BUY:
            return -(self.gross_local + self.fee + self.tax)
        if t == TxnType.SELL:
            return self.gross_local - self.fee - self.tax
        if t == TxnType.DIVIDEND:
            return self.gross_local - self.tax - self.fee
        if t in (TxnType.TRANSFER_IN, TxnType.TRANSFER_OUT):
            return -(self.fee + self.tax)
        return (self.amount or D0)  # cash-only: signed as given

    @property
    def cash_effect_base(self) -> Decimal:
        return self.cash_effect_local * self.fx_rate


@dataclass
class Position:
    isin: str
    name: str | None
    currency: str
    quantity: Decimal = D0
    cost_local: Decimal = D0        # gross cost basis of open quantity (excludes fees/taxes)
    cost_base: Decimal = D0         # same in base currency at trade-date effective FX
    realized_pl_base: Decimal = D0  # sells/transfers: gross proceeds − avg cost (fees excluded)
    realized_pl_local: Decimal = D0
    dividends_base: Decimal = D0    # net of withholding tax and fees
    dividends_gross_base: Decimal = D0
    dividend_tax_base: Decimal = D0
    fees_base: Decimal = D0         # trade + dividend + attributed standalone fees
    taxes_base: Decimal = D0        # attributed transaction taxes (TOB/FTT), net of refunds
    first_buy: date | None = None
    last_txn: date | None = None
    n_txns: int = 0
    broker_realized_pl_base: Decimal | None = None  # sum of broker-reported P/L, if any
    warnings: list[str] = field(default_factory=list)

    @property
    def avg_cost_local(self) -> Decimal:
        return self.cost_local / self.quantity if self.quantity else D0

    @property
    def avg_cost_base(self) -> Decimal:
        return self.cost_base / self.quantity if self.quantity else D0

    @property
    def is_open(self) -> bool:
        return self.quantity != 0

    @property
    def total_costs_base(self) -> Decimal:
        return self.fees_base + self.taxes_base


@dataclass
class Ledger:
    positions: dict[str, Position]
    cash_base: Decimal
    deposits_base: Decimal
    withdrawals_base: Decimal
    fees_base: Decimal          # every fee in the account
    taxes_base: Decimal         # transaction taxes + dividend withholding tax
    interest_base: Decimal
    income_base: Decimal
    dividends_net_base: Decimal
    realized_pl_base: Decimal
    other_cash_base: Decimal
    warnings: list[str] = field(default_factory=list)

    @property
    def net_invested_base(self) -> Decimal:
        return self.deposits_base - self.withdrawals_base

    @property
    def open_positions(self) -> dict[str, Position]:
        return {k: v for k, v in self.positions.items() if v.is_open}
