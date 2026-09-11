"""Pure, database-independent transaction and position types used by the engine.

The importer's job is to turn a broker file into a list of `Txn`; the engine never
sees broker-specific fields. Money is Decimal, never float.
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
    FEE = "fee"            # standalone fee/charge (not attached to a trade)
    INTEREST = "interest"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TAX = "tax"            # standalone tax charge/refund
    OTHER = "other"        # cash movement of unknown nature; affects cash only, flagged


SECURITY_TYPES = {TxnType.BUY, TxnType.SELL, TxnType.DIVIDEND}
EXTERNAL_FLOW_TYPES = {TxnType.DEPOSIT, TxnType.WITHDRAWAL}

D0 = Decimal("0")


@dataclass(frozen=True)
class Txn:
    """One normalised transaction.

    Conventions:
    - `quantity`, `price` are in the security's trading currency (`currency`).
    - `fee` and `tax` are in `currency` too, and are always >= 0.
    - `fx_rate` converts `currency` -> base currency: base_amount = local_amount * fx_rate.
      For base-currency rows it is 1.
    - `amount` is the signed cash effect in `currency` for non-trade rows (dividend gross,
      deposit, withdrawal, fee, interest). Deposits positive, withdrawals negative,
      fees negative. For BUY/SELL it may be None and is derived from quantity*price.
    - `id` is a stable identifier (e.g. hash of the source row) so re-imports are idempotent.
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

    # ---- derived, all in local currency unless suffixed _base ----
    @property
    def gross_local(self) -> Decimal:
        """Unsigned trade value or cash amount in local currency (before fees/taxes)."""
        if self.type in (TxnType.BUY, TxnType.SELL):
            return (self.quantity * self.price) if self.amount is None else abs(self.amount)
        return abs(self.amount or D0)

    @property
    def cash_effect_local(self) -> Decimal:
        """Signed effect on cash, local currency, including fees and taxes."""
        t = self.type
        if t == TxnType.BUY:
            return -(self.gross_local + self.fee + self.tax)
        if t == TxnType.SELL:
            return self.gross_local - self.fee - self.tax
        if t == TxnType.DIVIDEND:
            return self.gross_local - self.fee - self.tax
        if t == TxnType.FEE:
            return -(self.gross_local + self.tax)
        if t == TxnType.TAX:
            return -(self.gross_local)
        # deposit / withdrawal / interest / other: signed amount as given, minus any fee
        return (self.amount or D0) - self.fee - self.tax

    @property
    def cash_effect_base(self) -> Decimal:
        return self.cash_effect_local * self.fx_rate


@dataclass
class Lot:
    """Not used for average-cost accounting, kept for a future FIFO option."""
    date: date
    quantity: Decimal
    cost_local: Decimal
    cost_base: Decimal


@dataclass
class Position:
    isin: str
    name: str | None
    currency: str
    quantity: Decimal = D0
    cost_local: Decimal = D0        # total cost basis of open quantity, local ccy (incl. fees)
    cost_base: Decimal = D0         # same in base ccy at trade-date FX
    realized_pl_base: Decimal = D0  # from sells: proceeds − avg cost − fees, in base
    realized_pl_local: Decimal = D0
    dividends_base: Decimal = D0    # net dividends received (after tax), base ccy
    dividends_gross_base: Decimal = D0
    dividend_tax_base: Decimal = D0
    fees_base: Decimal = D0         # all fees attributable to this security (trade + dividend)
    first_buy: date | None = None
    last_txn: date | None = None
    n_txns: int = 0
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


@dataclass
class Ledger:
    positions: dict[str, Position]
    cash_base: Decimal
    deposits_base: Decimal
    withdrawals_base: Decimal
    fees_base: Decimal          # every fee in the account (trade, dividend, standalone)
    taxes_base: Decimal
    interest_base: Decimal
    dividends_net_base: Decimal
    realized_pl_base: Decimal
    other_cash_base: Decimal    # OTHER rows, so nothing is silently dropped
    warnings: list[str] = field(default_factory=list)

    @property
    def net_invested_base(self) -> Decimal:
        """Capital contributed by the owner: deposits − withdrawals."""
        return self.deposits_base - self.withdrawals_base

    @property
    def open_positions(self) -> dict[str, Position]:
        return {k: v for k, v in self.positions.items() if v.is_open}
