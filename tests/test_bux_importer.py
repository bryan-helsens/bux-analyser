from decimal import Decimal as D
from pathlib import Path

from bux_analyser.core.ledger import build_ledger
from bux_analyser.core.types import TxnType as T
from bux_analyser.db import ImportedRow, Transaction, make_engine, session_factory
from bux_analyser.importers.bux_csv import parse_bux_csv
from bux_analyser.importers.persist import import_bux_file, load_txns

FIX = Path(__file__).parent / "fixtures" / "bux_synthetic.csv"


def test_parse_synthetic_export():
    res = parse_bux_csv(FIX.read_text())
    assert res.row_count == 21 and not res.missing_columns and not res.unknown_columns
    assert res.cash_check.ok, res.cash_check.mismatches
    assert res.cash_check.final_balance == D("844.02")
    by = {}
    for t in res.txns:
        by.setdefault(t.type, []).append(t)
    assert len(by[T.BUY]) == 2 and len(by[T.SELL]) == 2 and len(by[T.DIVIDEND]) == 3
    assert len(by[T.FEE]) == 2 and len(by[T.TAX]) == 2 and len(by[T.DEPOSIT]) == 1 and len(by[T.WITHDRAWAL]) == 1
    assert len(by[T.INTEREST]) == 1 and len(by[T.INCOME]) == 1
    assert len(by[T.TRANSFER_IN]) == 1 and len(by[T.TRANSFER_OUT]) == 1
    assert T.OTHER not in by
    # USD buy: exact price from asset leg, effective fx from cash leg
    b = next(t for t in by[T.BUY] if t.isin == "US0000000001")
    assert b.quantity == D("2") and b.price == D("110") and b.currency == "USD"
    assert b.fx_rate == D("200") / D("220")
    # double-spaced 'Buy  Trade' cash leg paired with its asset leg
    b2 = next(t for t in by[T.BUY] if t.isin == "NL0000000002")
    assert b2.fx_rate == 1 and b2.amount == D("150")
    # dividend: gross/tax in USD, effective fx reproduces the EUR credited
    dv = next(t for t in by[T.DIVIDEND] if t.note == "cash dividend")
    assert dv.currency == "USD" and dv.amount == D("2.0") and dv.tax == D("0.3") and dv.fx_rate == D("1.7") / D("1.7")
    rv = next(t for t in by[T.DIVIDEND] if "reversal" in t.note)
    assert rv.amount == D("2.0") and rv.cash_effect_base == D("-1.7")
    # sell carries broker P/L; corporate-action pair became a SELL at cash/qty despite the wrong asset id
    s1 = next(t for t in by[T.SELL] if t.isin == "US0000000001")
    assert s1.broker_pl == D("10.0")
    ca = next(t for t in by[T.SELL] if t.isin == "NL0000000002")
    assert ca.quantity == D("10") and ca.price == D("18") and ca.amount == D("180") and "corporate action" in ca.note
    # merged rows: 3 trade cash legs + 1 corporate-action cash row
    assert len(res.merged_rows) == 4
    assert any("withdrawal" in w.lower() for w in res.warnings) and len(res.warnings) == 1
    assert res.securities["COIN"]["asset_type"] == "crypto" and res.securities["US0000000001"]["currency"] == "USD"


def test_ledger_from_synthetic_export():
    res = parse_bux_csv(FIX.read_text())
    l = build_ledger(res.txns)
    assert l.cash_base == D("844.02")
    acme = l.positions["US0000000001"]
    assert acme.quantity == D("1") and acme.avg_cost_local == D("110")
    assert acme.avg_cost_base == D("100")                      # 200 EUR / 2 shares
    assert acme.realized_pl_base == D("110") - D("100")        # matches broker P/L 10.0
    assert acme.fees_base == D("0.99") and acme.taxes_base == D("0.7") - D("0.2")
    assert acme.dividends_base == 0                            # dividend fully reversed
    dutch = l.positions["NL0000000002"]
    assert not dutch.is_open and dutch.realized_pl_base == D("30") and dutch.dividends_base == D("3")
    assert not l.positions["COIN"].is_open
    assert l.deposits_base == D("1000") and l.withdrawals_base == D("100")
    assert l.fees_base == D("0.99") + D("2.99") and l.interest_base == D("0.5") and l.income_base == D("5")
    assert not l.warnings


def test_persist_is_idempotent(tmp_path):
    eng = make_engine(tmp_path / "t.db")
    s = session_factory(eng)()
    r1 = import_bux_file(s, FIX, archive=False)
    assert not r1.already_imported and r1.new_rows == 21 and r1.new_txns == 17 and r1.cash_ok
    assert s.query(Transaction).count() == 17 and s.query(ImportedRow).count() == 21
    assert s.query(ImportedRow).filter_by(status="merged").count() == 4
    # same file again → nothing new
    r2 = import_bux_file(s, FIX, archive=False)
    assert r2.already_imported and s.query(Transaction).count() == 17
    # overlapping export with one extra row → only that row is added
    extra = FIX.read_text().rstrip("\n") + "\n2024-06-01 09:00:00.000000,deposits,Sepa Deposit,CASH_CREDIT,50.0,EUR,894.02,,,,,,,,,,,,,,\n"
    f2 = tmp_path / "later.csv"
    f2.write_text(extra)
    r3 = import_bux_file(s, f2, archive=False)
    assert r3.new_rows == 1 and r3.new_txns == 1 and s.query(Transaction).count() == 18
    # round-trip through the database preserves Decimal semantics
    txns = load_txns(s)
    assert build_ledger(txns).cash_base == D("894.02")
    assert next(t for t in txns if t.isin == "US0000000001" and t.type == T.SELL).broker_pl == D("10.0")
