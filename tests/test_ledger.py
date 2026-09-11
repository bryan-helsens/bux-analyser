from datetime import date
from decimal import Decimal as D

from bux_analyser.core.ledger import build_ledger, holdings_on
from bux_analyser.core.types import Txn, TxnType as T


def tx(i, type, d, **kw):
    base = dict(currency="EUR", fx_rate=D("1"))
    base.update(kw)
    return Txn(id=f"t{i}", type=type, date=date(*d), **base)


def test_deposit_buy_sell_realized_and_cash():
    txns = [
        tx(1, T.DEPOSIT, (2024, 1, 1), amount=D("1000")),
        tx(2, T.BUY, (2024, 1, 2), isin="NL0010273215", name="ASML", quantity=D("2"), price=D("300"), fee=D("1")),
        tx(3, T.SELL, (2024, 2, 1), isin="NL0010273215", quantity=D("1"), price=D("350"), fee=D("1")),
    ]
    l = build_ledger(txns)
    p = l.positions["NL0010273215"]
    assert p.quantity == D("1")
    assert p.avg_cost_local == D("300.5")            # (600 + 1 fee) / 2
    assert p.realized_pl_base == D("48.5")           # 349 net proceeds − 300.5
    assert l.realized_pl_base == D("48.5")
    assert l.cash_base == D("1000") - D("601") + D("349")
    assert l.fees_base == D("2")
    assert l.net_invested_base == D("1000")


def test_multiple_buys_average_cost_and_partial_sells():
    txns = [
        tx(1, T.BUY, (2024, 1, 1), isin="X", quantity=D("10"), price=D("10")),
        tx(2, T.BUY, (2024, 1, 2), isin="X", quantity=D("10"), price=D("20")),
        tx(3, T.SELL, (2024, 1, 3), isin="X", quantity=D("5"), price=D("30")),
        tx(4, T.SELL, (2024, 1, 4), isin="X", quantity=D("15"), price=D("10")),
    ]
    l = build_ledger(txns)
    p = l.positions["X"]
    assert p.quantity == 0 and not p.is_open
    assert p.cost_base == 0
    # avg cost 15: sell 5@30 → +75 ; sell 15@10 → −75 ; total 0
    assert p.realized_pl_base == D("0")
    assert holdings_on(txns, date(2024, 1, 3)) == {"X": D("15")}
    assert holdings_on(txns, date(2024, 1, 4)) == {}


def test_fractional_shares_and_dust():
    txns = [
        tx(1, T.BUY, (2024, 1, 1), isin="X", quantity=D("0.33333333"), price=D("90")),
        tx(2, T.BUY, (2024, 1, 2), isin="X", quantity=D("0.66666667"), price=D("90")),
        tx(3, T.SELL, (2024, 1, 3), isin="X", quantity=D("1"), price=D("100")),
    ]
    p = build_ledger(txns).positions["X"]
    assert p.quantity == 0 and p.cost_base == 0
    assert p.realized_pl_base == D("10.000000")


def test_dividend_with_withholding_tax_and_fx():
    txns = [
        tx(1, T.BUY, (2024, 1, 1), isin="US0378331005", currency="USD", fx_rate=D("0.9"), quantity=D("10"), price=D("100"), fee=D("1")),
        tx(2, T.DIVIDEND, (2024, 3, 1), isin="US0378331005", currency="USD", fx_rate=D("0.92"), amount=D("5"), tax=D("0.75")),
    ]
    l = build_ledger(txns)
    p = l.positions["US0378331005"]
    assert p.cost_base == D("1001") * D("0.9")
    assert p.avg_cost_local == D("100.1")
    assert p.dividends_gross_base == D("5") * D("0.92")
    assert p.dividend_tax_base == D("0.75") * D("0.92")
    assert p.dividends_base == D("4.25") * D("0.92")
    assert l.dividends_net_base == p.dividends_base
    assert l.taxes_base == D("0.75") * D("0.92")
    assert l.cash_base == -D("1001") * D("0.9") + D("4.25") * D("0.92")


def test_standalone_fee_withdrawal_interest_other():
    txns = [
        tx(1, T.DEPOSIT, (2024, 1, 1), amount=D("500")),
        tx(2, T.FEE, (2024, 1, 5), amount=D("2.5")),
        tx(3, T.WITHDRAWAL, (2024, 1, 6), amount=D("-100")),
        tx(4, T.INTEREST, (2024, 1, 7), amount=D("1.2")),
        tx(5, T.OTHER, (2024, 1, 8), amount=D("-3"), note="promo"),
    ]
    l = build_ledger(txns)
    assert l.deposits_base == D("500") and l.withdrawals_base == D("100")
    assert l.fees_base == D("2.5") and l.interest_base == D("1.2") and l.other_cash_base == D("-3")
    assert l.cash_base == D("500") - D("2.5") - D("100") + D("1.2") - D("3")
    assert l.net_invested_base == D("400")
    assert any("OTHER" in w for w in l.warnings)


def test_oversell_is_clamped_and_warned():
    txns = [
        tx(1, T.BUY, (2024, 1, 1), isin="X", quantity=D("1"), price=D("10")),
        tx(2, T.SELL, (2024, 1, 2), isin="X", quantity=D("2"), price=D("10")),
    ]
    l = build_ledger(txns)
    assert l.positions["X"].quantity == 0
    assert any("exceeds held" in w for w in l.warnings)


def test_empty_and_order_independence():
    assert build_ledger([]).positions == {}
    a = [tx(1, T.BUY, (2024, 1, 1), isin="X", quantity=D("1"), price=D("10")),
         tx(2, T.SELL, (2024, 1, 2), isin="X", quantity=D("1"), price=D("12"))]
    assert build_ledger(a).realized_pl_base == build_ledger(list(reversed(a))).realized_pl_base == D("2")
