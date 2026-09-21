"""Reconciliation report: one command that shows everything needed to verify the
pipeline against the BUX app, and compares our numbers with figures read from it.

Pure formatting over a Snapshot plus an optional figures file, so it is testable
without a network or a database.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import ImportBatch, Transaction
from .service import Snapshot

TEMPLATE_COLUMNS = ["isin", "name", "quantity", "avg_price", "value_eur"]
QTY_TOL = Decimal("0.000001")
PRICE_REL_TOL = Decimal("0.001")
PRICE_ABS_TOL = Decimal("0.005")
CASH_TOL = Decimal("0.01")
VALUE_REL_TOL = Decimal("0.005")
VALUE_ABS_TOL = Decimal("1")


@dataclass
class Figures:
    """Numbers typed in from the BUX app. Blank cells mean 'do not check'."""
    holdings: dict[str, dict[str, Decimal | None]] = field(default_factory=dict)
    cash: Decimal | None = None
    total: Decimal | None = None
    as_of: str = ""
    problems: list[str] = field(default_factory=list)


def parse_number(raw: str) -> Decimal | None:
    """Accept 1234.56, 1.234,56, 1,234.56, '€ 1 234,56'. Blank -> None."""
    s = re.sub(r"[^0-9,.\-]", "", (raw or "").strip())
    if s in ("", "-"):
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rindex(".") > s.rindex(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head}.{tail}" if len(tail) != 3 or "," in head else head + tail
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_figures(text: str) -> Figures:
    """Read the filled-in template. Delimiter may be ',' or ';' (Dutch Excel)."""
    fig = Figures()
    body = "\n".join(ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))
    if not body.strip():
        return fig
    delim = ";" if body.count(";") > body.count(",") else ","
    for n, row in enumerate(csv.DictReader(io.StringIO(body), delimiter=delim), start=1):
        keys = {re.sub(r"[^a-z]", "", (k or "").lower()): (v or "").strip() for k, v in row.items()}
        key = (keys.get("isin") or "").upper()
        if not key or key == "ISIN":
            continue
        qty, price, value = (parse_number(keys.get(c, "")) for c in ("quantity", "avgprice", "valueeur"))
        if key in ("CASH", "TOTAL"):
            amount = value if value is not None else (price if price is not None else qty)
            if amount is None:
                fig.problems.append(f"row {n}: {key} has no amount")
            elif key == "CASH":
                fig.cash = amount
            else:
                fig.total = amount
            continue
        if key == "AS_OF":
            fig.as_of = keys.get("name") or keys.get("quantity") or ""
            continue
        if qty is None and price is None and value is None:
            continue  # row left blank
        fig.holdings[key] = {"quantity": qty, "avg_price": price, "value": value}
    return fig


def write_template(snapshot: Snapshot, path: Path) -> Path:
    lines = [
        "# BUX figures — fill in from the BUX app, then run:",
        "#     python -m bux_analyser.cli reconcile " + str(path),
        "# One row per open holding. Leave a cell blank to skip that check.",
        "# Decimal comma or point both work. Delimiter , or ; both work.",
        "#",
        ",".join(TEMPLATE_COLUMNS),
    ]
    for h in sorted(snapshot.holdings, key=lambda x: (x.name or "").lower()):
        lines.append(f"{h.isin},{(h.name or '').replace(',', ' ')},,,")
    lines += ["CASH,cash balance in the app,,,", "TOTAL,total portfolio value in the app,,,",
              "AS_OF,YYYY-MM-DD when you read these,,,"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fmt(x, digits=2, dash="n/a"):
    if x is None:
        return dash
    return f"{float(x):,.{digits}f}"


def _table(rows: list[list[str]], headers: list[str]) -> list[str]:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    sep = "  "
    out = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append(sep.join("-" * w for w in widths))
    out += [sep.join(str(r[i]).ljust(widths[i]) for i in range(len(headers))) for r in rows]
    return out


def compare(snapshot: Snapshot, fig: Figures) -> tuple[list[str], int]:
    """Compare our numbers with the app's. Returns (lines, problem_count)."""
    lines: list[str] = []
    problems = 0
    ours = {h.isin: h for h in snapshot.holdings}
    rows = []
    for isin in sorted(set(ours) | set(fig.holdings)):
        h, f = ours.get(isin), fig.holdings.get(isin)
        name = (h.name if h else "") or isin
        if f is None:
            rows.append([name[:24], isin, "held", "not in file", "-"])
            continue
        if h is None:
            rows.append([name[:24], isin, "not held", "in file", "MISMATCH"])
            problems += 1
            continue
        marks = []
        if f["quantity"] is not None:
            d = (h.quantity - f["quantity"]).copy_abs()
            marks.append(("qty ok" if d <= QTY_TOL else f"qty ours {h.quantity} vs BUX {f['quantity']}"))
            problems += d > QTY_TOL
        if f["avg_price"] is not None:
            tol = max(PRICE_ABS_TOL, PRICE_REL_TOL * f["avg_price"].copy_abs())
            d = (h.avg_cost_local - f["avg_price"]).copy_abs()
            marks.append("avg ok" if d <= tol else f"avg ours {_fmt(h.avg_cost_local, 4)} vs BUX {_fmt(f['avg_price'], 4)}")
            problems += d > tol
        # Quantity and average cost come purely from the export and must match exactly.
        # Value depends on a live price, so a difference is reported but not counted as a
        # problem: delayed quotes and ECB reference rates legitimately differ from the app.
        if f["value"] is not None and h.value_base is not None:
            d = abs(Decimal(str(h.value_base)) - f["value"])
            tol = max(VALUE_ABS_TOL, VALUE_REL_TOL * f["value"].copy_abs())
            marks.append("value ok" if d <= tol else f"value ours {_fmt(h.value_base)} vs BUX {_fmt(f['value'])} (diff {_fmt(d)})")
        ok = all(m.endswith("ok") for m in marks)
        rows.append([name[:24], isin, f"{h.quantity}", _fmt(h.avg_cost_local, 4),
                     "OK" if ok and marks else ("no figures" if not marks else "MISMATCH")])
        if not ok:
            for m in marks:
                if not m.endswith("ok"):
                    lines.append(f"    {name}: {m}")
    body = _table(rows, ["NAME", "ISIN", "OUR QTY", "OUR AVG COST", "CHECK"])
    detail = lines
    lines = ["[6] COMPARISON WITH THE BUX APP" + (f"  (figures as of {fig.as_of})" if fig.as_of else ""), ""]
    lines += ["  " + b for b in body]
    if detail:
        lines += ["", "  differences:"] + detail
    lines.append("")
    if fig.cash is not None:
        d = abs(Decimal(str(snapshot.cash)) - fig.cash)
        ok = d <= CASH_TOL
        problems += not ok
        lines.append(f"  cash:  ours {_fmt(snapshot.cash)}  BUX {_fmt(fig.cash)}  -> {'OK' if ok else f'MISMATCH (diff {_fmt(d)})'}")
    if fig.total is not None and snapshot.total_value is not None:
        d = abs(Decimal(str(snapshot.total_value)) - fig.total)
        tol = max(VALUE_ABS_TOL, VALUE_REL_TOL * fig.total.copy_abs())
        lines.append(f"  total: ours {_fmt(snapshot.total_value)}  BUX {_fmt(fig.total)}  -> "
                     + ("OK" if d <= tol else f"differs by {_fmt(d)} ({float(d / fig.total):.2%})"))
        if d > tol:
            lines.append("         a small difference is expected: delayed quotes and ECB reference rates")
            lines.append("         versus the app's live price and intraday rate.")
    for p in fig.problems:
        lines.append(f"  ! {p}")
    return lines, problems


def build_report(session: Session, snapshot: Snapshot, store, fig: Figures | None = None) -> str:
    L: list[str] = []
    L.append("BUX ANALYSER - RECONCILIATION REPORT")
    L.append(f"generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | base currency EUR")
    L.append("")

    batches = session.execute(select(ImportBatch).order_by(ImportBatch.imported_at)).scalars().all()
    n_txn = session.execute(select(func.count()).select_from(Transaction)).scalar()
    dates = session.execute(select(func.min(Transaction.date), func.max(Transaction.date))).one()
    L.append("[1] IMPORT")
    L.append(f"  {n_txn} transactions from {len(batches)} import(s); dates {dates[0]} .. {dates[1]}")
    for b in batches[-3:]:
        L.append(f"  {b.filename}: {b.row_count} rows, {b.new_rows} new, imported {b.imported_at:%Y-%m-%d %H:%M}")
        for w in (b.warnings or "").splitlines():
            L.append(f"    - {w}")
    L.append("")

    L.append("[2] MARKET DATA PROVIDERS")
    for h in store.router.health():
        L.append(f"  {h.name:8} {h.calls:4} calls  {h.failures:3} failures" + (f"  last error: {h.last_error}" if h.last_error else ""))
    L.append("")

    L.append(f"[3] SYMBOL RESOLUTION  ({len(snapshot.holdings)} open holdings)")
    rows = []
    for h in sorted(snapshot.holdings, key=lambda x: (x.name or "").lower()):
        p = h.price_provenance
        rows.append([(h.name or "")[:24], h.isin, h.currency or "?", h.ticker or "UNRESOLVED",
                     h.price_currency or "?", _fmt(h.price, 2), (p.data_date.isoformat() if p and p.data_date else "-"),
                     (p.provider if p else "-")])
    L += ["  " + r for r in _table(rows, ["NAME", "ISIN", "TRADED", "SYMBOL", "QUOTE", "PRICE", "DATA DATE", "SOURCE"])]
    flagged = [h for h in snapshot.holdings if h.issues]
    if flagged:
        L.append("")
        L.append("  flagged:")
        for h in flagged:
            for i in h.issues:
                L.append(f"    {h.name}: {i}")
    L.append("")

    L.append("[4] HOLDINGS")
    rows = []
    for h in sorted(snapshot.holdings, key=lambda x: -(x.value_base or 0)):
        rows.append([(h.name or "")[:24], f"{h.quantity}", f"{_fmt(h.avg_cost_local, 4)} {h.currency}",
                     _fmt(h.value_base), _fmt(h.unrealized_base),
                     (f"{h.unrealized_pct:+.1%}" if h.unrealized_pct is not None else "n/a"),
                     (f"{h.weight:.1%}" if h.weight is not None else "n/a"),
                     _fmt(h.dividends_base), _fmt(h.fees_base + h.taxes_base)])
    L += ["  " + r for r in _table(rows, ["NAME", "QTY", "AVG COST", "VALUE EUR", "P/L EUR", "P/L %", "WEIGHT", "DIVID", "COSTS"])]
    L.append("")

    led = snapshot.ledger
    invested = float(led.net_invested_base)
    total_pl = (snapshot.total_value - invested) if snapshot.total_value is not None else None
    L.append("[5] TOTALS")
    L.append(f"  securities      {_fmt(snapshot.securities_value):>12}")
    L.append(f"  cash            {_fmt(snapshot.cash):>12}   (reconciled against BUX's running balance at import)")
    L.append(f"  total value     {_fmt(snapshot.total_value):>12}")
    L.append(f"  net invested    {_fmt(invested):>12}   (deposits - withdrawals)")
    L.append(f"  total P/L       {_fmt(total_pl):>12}" + (f"   ({total_pl / invested:+.2%})" if total_pl is not None and invested else ""))
    L.append(f"  unrealized P/L  {_fmt(snapshot.unrealized):>12}")
    L.append(f"  realized P/L    {_fmt(led.realized_pl_base):>12}")
    L.append(f"  dividends net   {_fmt(led.dividends_net_base):>12}")
    L.append(f"  interest+income {_fmt(led.interest_base + led.income_base):>12}")
    L.append(f"  fees            {_fmt(-led.fees_base):>12}")
    L.append(f"  taxes           {_fmt(-led.taxes_base):>12}")
    if not snapshot.twr.empty:
        L.append(f"  time-weighted   {snapshot.twr.iloc[-1] - 1:>+11.2%}   since {snapshot.history.index[0].date()}")
    L.append(f"  money-weighted  {(f'{snapshot.mwr:+.2%}' if snapshot.mwr is not None else 'n/a'):>12}   (XIRR, annualised)")
    L.append("")

    if snapshot.warnings:
        L.append("[!] WARNINGS")
        for w in snapshot.warnings:
            L.append(f"  - {w}")
        L.append("")

    if fig is not None:
        cmp_lines, problems = compare(snapshot, fig)
        L += cmp_lines
        L.append("")
        L.append(f"  => {problems} mismatch(es) needing attention" if problems else "  => everything checked matches the BUX app")
    else:
        L.append("[6] COMPARISON WITH THE BUX APP")
        L.append("  No figures supplied. Create a template with:")
        L.append("      python -m bux_analyser.cli reconcile --template")
        L.append("  fill it in from the app, then rerun with the filled file as the argument.")
    return "\n".join(L) + "\n"
