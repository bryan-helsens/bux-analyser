"""BUX transaction-history CSV importer. See docs/bux-import-spec.md for the format.

parse_bux_csv(text) -> ParseResult(txns, warnings, cash_check, securities)
Pure: no database, no network. Persisting is in `bux_analyser.importers.persist`.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..core.types import D0, Txn, TxnType

EXPECTED_COLUMNS = [
    "Transaction Time (CET)", "Transaction Category", "Transaction Type", "Transfer Type",
    "Transaction Amount", "Transaction Currency", "Cash Balance Amount", "Asset Id", "Asset Name",
    "Asset Quantity", "Asset Price", "Asset Currency", "Currency Pair", "Exchange Rate",
    "Profit And Loss Amount", "Profit And Loss Currency", "Dividend Currency",
    "Dividend Gross Amount", "Dividend Net Amount", "Dividend Tax Amount", "Transaction Description",
]
_norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
_KEYS = {_norm(c): c for c in EXPECTED_COLUMNS}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_MAX_QTY_ISSUE = 8  # decimals


@dataclass
class Row:
    idx: int
    raw: dict[str, str]
    rid: str
    time: datetime
    category: str
    ttype: str        # normalised: lower, single-spaced
    transfer: str
    amount: Decimal
    ccy: str
    balance: Decimal | None
    asset_id: str
    asset_name: str
    qty: Decimal | None
    price: Decimal | None
    asset_ccy: str
    pair: str
    rate: Decimal | None
    pl: Decimal | None
    div_ccy: str
    div_gross: Decimal | None
    div_net: Decimal | None
    div_tax: Decimal | None
    desc: str
    used: bool = False

    @property
    def uuid(self) -> str | None:
        m = _UUID.search(self.desc)
        return m.group(0) if m else None


@dataclass
class CashCheck:
    rows_checked: int = 0
    mismatches: list[str] = field(default_factory=list)
    final_balance: Decimal | None = None

    @property
    def ok(self) -> bool:
        return not self.mismatches


@dataclass
class ParseResult:
    txns: list[Txn]
    warnings: list[str]
    cash_check: CashCheck
    securities: dict[str, dict]          # isin -> {name, currency, asset_type}
    merged_rows: dict[str, str]          # source row id -> txn id (rows folded into another txn)
    row_ids: list[str]                   # every source row id, in file order
    row_count: int
    unknown_columns: list[str]
    missing_columns: list[str]


def _dec(s: str) -> Decimal | None:
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _read_rows(text: str) -> tuple[list[Row], list[str], list[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = reader.fieldnames or []
    mapping = {h: _KEYS.get(_norm(h)) for h in headers}
    unknown = [h for h, k in mapping.items() if k is None]
    missing = [c for c in EXPECTED_COLUMNS if c not in mapping.values()]
    rows: list[Row] = []
    for i, r in enumerate(reader, start=2):
        raw = {mapping[h] or h: (v or "").strip() for h, v in r.items() if h is not None}
        g = lambda k: raw.get(k, "")
        rid = hashlib.sha1("|".join(g(c) for c in EXPECTED_COLUMNS).encode()).hexdigest()
        ts = g("Transaction Time (CET)")
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            raise ValueError(f"line {i}: unparseable timestamp {ts!r}")
        rows.append(Row(
            idx=i, raw=raw, rid=rid, time=t, category=g("Transaction Category").lower(),
            ttype=" ".join(g("Transaction Type").lower().split()), transfer=g("Transfer Type").upper(),
            amount=_dec(g("Transaction Amount")) or D0, ccy=g("Transaction Currency").upper(),
            balance=_dec(g("Cash Balance Amount")), asset_id=g("Asset Id"), asset_name=g("Asset Name"),
            qty=_dec(g("Asset Quantity")), price=_dec(g("Asset Price")), asset_ccy=g("Asset Currency").upper(),
            pair=g("Currency Pair").upper(), rate=_dec(g("Exchange Rate")), pl=_dec(g("Profit And Loss Amount")),
            div_ccy=g("Dividend Currency").upper(), div_gross=_dec(g("Dividend Gross Amount")),
            div_net=_dec(g("Dividend Net Amount")), div_tax=_dec(g("Dividend Tax Amount")),
            desc=g("Transaction Description"),
        ))
    return rows, unknown, missing


def parse_bux_csv(text: str) -> ParseResult:
    rows, unknown, missing = _read_rows(text)
    warnings: list[str] = []
    if unknown:
        warnings.append(f"Unknown columns ignored: {unknown}")
    if missing:
        warnings.append(f"Expected columns missing (import may be incomplete): {missing}")
    txns: list[Txn] = []
    securities: dict[str, dict] = {}
    merged_rows: dict[str, str] = {}   # source row id -> txn id it was merged into (trade cash legs, CA cash)

    def note_security(r: Row, trading: bool) -> None:
        s = securities.setdefault(r.asset_id, {"name": r.asset_name, "currency": None,
                                                "asset_type": "security" if _ISIN.match(r.asset_id) else "crypto"})
        if trading and r.asset_ccy and not s["currency"]:
            s["currency"] = r.asset_ccy
        if not s["name"] and r.asset_name:
            s["name"] = r.asset_name

    # ---- 1. trades: pair asset leg + cash leg -------------------------------
    trade_rows = [r for r in rows if r.category == "trades"]
    asset_legs = [r for r in trade_rows if r.transfer in ("ASSET_TRADE_BUY", "ASSET_TRADE_SELL")]
    cash_legs = [r for r in trade_rows if r.transfer in ("CASH_DEBIT", "CASH_CREDIT")]

    def find_cash_leg(a: Row) -> Row | None:
        cands = [c for c in cash_legs if not c.used and c.asset_id == a.asset_id]
        if a.uuid:
            by_uuid = [c for c in cands if c.uuid == a.uuid]
            if by_uuid:
                return by_uuid[0]
        near = [c for c in cands if c.qty == a.qty and abs(c.time - a.time) <= timedelta(seconds=2)]
        return near[0] if near else None

    for a in asset_legs:
        is_buy = a.transfer == "ASSET_TRADE_BUY"
        c = find_cash_leg(a)
        a.used = True
        gross_local = abs(a.amount)
        qty = a.qty or D0
        if qty <= 0 or gross_local <= 0:
            warnings.append(f"line {a.idx}: trade leg without quantity/amount; skipped")
            continue
        if c is not None:
            c.used = True
            merged_rows[c.rid] = a.rid
            cash_eur = abs(c.amount)
            fx = (cash_eur / gross_local) if gross_local else Decimal("1")
            pl = c.pl if c.pl is not None else a.pl
        else:
            warnings.append(f"line {a.idx}: {a.ttype} {a.asset_id} asset leg without cash leg; "
                            f"using nominal rate {a.rate or 1}")
            fx = (Decimal("1") / a.rate) if a.rate else Decimal("1")
            pl = a.pl
        note_security(a, trading=True)
        txns.append(Txn(id=a.rid, type=TxnType.BUY if is_buy else TxnType.SELL, date=a.time.date(),
                        currency=a.asset_ccy or a.ccy, fx_rate=fx, isin=a.asset_id, name=a.asset_name,
                        quantity=qty, price=(a.price if a.price is not None else gross_local / qty),
                        amount=gross_local, note=a.desc, broker_pl=pl))
    for c in cash_legs:
        if c.used:
            continue
        c.used = True
        qty, price = c.qty or D0, c.price or D0
        if qty <= 0:
            warnings.append(f"line {c.idx}: trade cash leg without asset leg or quantity; kept as OTHER")
            txns.append(Txn(id=c.rid, type=TxnType.OTHER, date=c.time.date(), currency=c.ccy, amount=c.amount, note=f"{c.ttype} {c.asset_name} {c.desc}"))
            continue
        warnings.append(f"line {c.idx}: {c.ttype} {c.asset_id} cash leg without asset leg; using rounded price")
        gross_local = qty * price
        fx = abs(c.amount) / gross_local if gross_local else Decimal("1")
        note_security(c, trading=True)
        txns.append(Txn(id=c.rid, type=TxnType.BUY if c.transfer == "CASH_DEBIT" else TxnType.SELL,
                        date=c.time.date(), currency=c.asset_ccy or c.ccy, fx_rate=fx, isin=c.asset_id,
                        name=c.asset_name, quantity=qty, price=price, amount=gross_local, note=c.desc, broker_pl=c.pl))

    # ---- 2. corporate actions & transfers ------------------------------------
    ca_rows = [r for r in rows if r.category == "corporate_actions" or r.ttype == "portfolio transfer"]
    redeems = [r for r in ca_rows if r.transfer == "ASSET_REDEEM"]
    for c in [r for r in ca_rows if r.transfer == "CASH_CREDIT"]:
        match = None
        for rd in sorted(redeems, key=lambda x: x.time, reverse=True):
            if rd.used or rd.time > c.time or c.time - rd.time > timedelta(days=30):
                continue
            if rd.asset_id == c.asset_id or (rd.asset_name and rd.asset_name.lower() in c.desc.lower()):
                match = rd
                break
        c.used = True
        if match is None:
            warnings.append(f"line {c.idx}: corporate-action cash {c.amount} {c.ccy} '{c.desc}' has no matching share redemption; booked as INCOME")
            txns.append(Txn(id=c.rid, type=TxnType.INCOME, date=c.time.date(), currency=c.ccy, amount=c.amount, note=f"{c.ttype}: {c.desc} ({c.asset_name})"))
            continue
        match.used = True
        qty = match.qty or D0
        note_security(match, trading=False)
        txns.append(Txn(id=match.rid, type=TxnType.SELL, date=match.time.date(), currency=c.ccy, fx_rate=Decimal("1"),
                        isin=match.asset_id, name=match.asset_name, quantity=qty, price=(c.amount / qty if qty else D0),
                        amount=c.amount, note=f"corporate action: {c.desc or match.desc}", broker_pl=match.pl))
        merged_rows[c.rid] = match.rid
    for r in ca_rows:
        if r.used or r.transfer not in ("ASSET_REDEEM", "ASSET_DEPOSIT"):
            continue
        r.used = True
        qty = r.qty or D0
        if qty <= 0:
            warnings.append(f"line {r.idx}: {r.ttype} without quantity; skipped")
            continue
        note_security(r, trading=False)
        out = r.transfer == "ASSET_REDEEM"
        fx = (Decimal("1") / r.rate) if r.rate else Decimal("1")
        txns.append(Txn(id=r.rid, type=TxnType.TRANSFER_OUT if out else TxnType.TRANSFER_IN, date=r.time.date(),
                        currency=r.asset_ccy or r.ccy, fx_rate=fx, isin=r.asset_id, name=r.asset_name, quantity=qty,
                        price=r.price or (abs(r.amount) / qty), amount=abs(r.amount), note=f"{r.ttype}: {r.desc}", broker_pl=r.pl))

    # ---- 3. everything else is a single cash row -------------------------------
    seen_withdrawal = False
    for r in rows:
        if r.used:
            continue
        r.used = True
        d = r.time.date()
        cat, tt = r.category, r.ttype
        if cat == "dividends":
            if not r.asset_id:
                warnings.append(f"line {r.idx}: dividend without asset id; booked as INCOME")
                txns.append(Txn(id=r.rid, type=TxnType.INCOME, date=d, currency=r.ccy, amount=r.amount, note=tt))
                continue
            ccy = r.div_ccy or r.asset_ccy or r.ccy
            gross, net, tax = r.div_gross, r.div_net, r.div_tax
            if gross is None or net is None:
                gross, net, tax = r.amount, r.amount, D0
                ccy = r.ccy
            if net:
                fx = r.amount / net
            elif r.rate:
                fx = Decimal("1") / r.rate
            else:
                fx = Decimal("1")
            note_security(r, trading=False)
            txns.append(Txn(id=r.rid, type=TxnType.DIVIDEND, date=d, currency=ccy, fx_rate=fx, isin=r.asset_id,
                            name=r.asset_name, amount=gross, tax=(tax or D0), note=tt))
            continue
        if cat == "fees":
            txns.append(Txn(id=r.rid, type=TxnType.FEE, date=d, currency=r.ccy, amount=r.amount,
                            isin=r.asset_id or None, name=r.asset_name or None, note=f"{tt} {r.desc}".strip()))
            if r.asset_id:
                note_security(r, trading=False)
            continue
        if cat == "tax":
            txns.append(Txn(id=r.rid, type=TxnType.TAX, date=d, currency=r.ccy, amount=r.amount,
                            isin=r.asset_id or None, name=r.asset_name or None, note=tt))
            if r.asset_id:
                note_security(r, trading=False)
            continue
        if cat == "interest":
            txns.append(Txn(id=r.rid, type=TxnType.INTEREST, date=d, currency=r.ccy, amount=r.amount, note=f"{tt} {r.desc}".strip()))
            continue
        if cat == "deposits" or "deposit" in tt:
            txns.append(Txn(id=r.rid, type=TxnType.DEPOSIT, date=d, currency=r.ccy, amount=r.amount, note=tt))
            continue
        if "withdraw" in cat or "withdraw" in tt:
            if not seen_withdrawal:
                warnings.append(f"line {r.idx}: first withdrawal-type row seen ({tt}); verify mapping")
                seen_withdrawal = True
            txns.append(Txn(id=r.rid, type=TxnType.WITHDRAWAL, date=d, currency=r.ccy, amount=r.amount, note=tt))
            continue
        if cat == "others" and tt in ("security lending revenue", "promotional"):
            txns.append(Txn(id=r.rid, type=TxnType.INCOME, date=d, currency=r.ccy, amount=r.amount, note=tt))
            continue
        warnings.append(f"line {r.idx}: unrecognised row {cat}/{tt}/{r.transfer} {r.amount} {r.ccy}; booked as OTHER")
        txns.append(Txn(id=r.rid, type=TxnType.OTHER, date=d, currency=r.ccy, amount=r.amount, note=f"{cat}/{tt}/{r.transfer} {r.asset_name} {r.desc}"))

    # ---- 4. cash reconciliation against the broker's running balance --------------
    # BUX books same-millisecond rows in a different order than it exports them, so the
    # check is order-independent: every row's (balance − amount) must be the balance of
    # exactly one other row (or the opening 0), i.e. the balances form one unbroken chain.
    check = CashCheck()
    cash_rows = [r for r in rows if r.transfer in ("CASH_CREDIT", "CASH_DEBIT")]
    total = sum((r.amount for r in cash_rows), D0)
    with_bal = [r for r in cash_rows if r.balance is not None]
    check.rows_checked = len(with_bal)
    if with_bal:
        q = lambda x: x.quantize(Decimal("0.01"))
        bals = Counter(q(r.balance) for r in with_bal)
        befores = Counter(q(r.balance - r.amount) for r in with_bal)
        expected = bals.copy()
        expected[q(total)] -= 1                      # the final balance is nobody's "before"
        expected[Decimal("0.00")] += 1               # the opening balance
        diff = (befores - expected) + (expected - befores)
        for v, n in diff.items():
            check.mismatches.append(f"cash chain broken around balance {v} ({n} row(s))")
        if q(total) not in bals:
            check.mismatches.append(f"sum of cash rows {q(total)} is not a reported balance")
    check.final_balance = total
    from ..core.ledger import build_ledger
    ledger_cash = build_ledger(txns).cash_base
    if abs(ledger_cash - total) > Decimal("0.005"):
        check.mismatches.append(f"ledger cash {ledger_cash:.2f} differs from sum of BUX cash rows {total:.2f}")

    return ParseResult(txns=txns, warnings=warnings, cash_check=check, securities=securities,
                       merged_rows=merged_rows, row_ids=[r.rid for r in rows], row_count=len(rows), unknown_columns=unknown, missing_columns=missing)
