"""Command line for bux-analyser. Everything runs locally.

    python -m bux_analyser.cli import <file.csv>        import a BUX transaction export
    python -m bux_analyser.cli refresh                  fetch prices and FX rates
    python -m bux_analyser.cli status                   short holdings overview
    python -m bux_analyser.cli set-ticker ISIN SYMBOL   override a resolved symbol
    python -m bux_analyser.cli reconcile [figures.csv]  full verification report
    python -m bux_analyser.cli reconcile --template     write a figures template to fill in
"""
from __future__ import annotations

import sys
from pathlib import Path

from .config import DATA_DIR
from .db import make_engine, session_factory
from .importers.persist import import_bux_file
from .reconcile import Figures, build_report, parse_figures, write_template
from .service import build_snapshot, default_store, refresh_market_data


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    s = session_factory(make_engine())()
    cmd, args = argv[0], argv[1:]

    if cmd == "import":
        if not args:
            print("usage: import <file.csv>")
            return 1
        r = import_bux_file(s, args[0])
        print(f"{r.filename}: {r.rows} rows, {r.new_rows} new, {r.new_txns} transactions"
              + (" (already imported)" if r.already_imported else ""))
        print("cash reconciliation:", "OK" if r.cash_ok else "MISMATCH")
        for m in r.cash_messages:
            print("  !", m)
        for w in r.warnings:
            print("  -", w)
        return 0 if r.cash_ok else 2

    if cmd == "refresh":
        store = default_store(s)
        for w in refresh_market_data(s, store):
            print("  -", w)
        for h in store.router.health():
            print(f"{h.name}: {h.calls} calls, {h.failures} failures {h.last_error}")
        return 0

    if cmd == "set-ticker":
        if len(args) != 2:
            print("usage: set-ticker <ISIN> <SYMBOL>")
            return 1
        default_store(s).set_manual_ticker(args[0], args[1])
        print(f"{args[0]} -> {args[1]} (cached prices cleared; run refresh)")
        return 0

    if cmd == "status":
        snap = build_snapshot(s, default_store(s))
        print(f"cash EUR {snap.cash:.2f}  securities {snap.securities_value}  total {snap.total_value}  MWR {snap.mwr}")
        for h in snap.holdings:
            print(f"{h.isin} {(h.name or '')[:26]:26} {h.quantity:>12} @ {h.avg_cost_local:>9.4f} {h.currency}"
                  f"  price {h.price} {h.price_currency}  value {h.value_base}  {h.issues}")
        for w in snap.warnings:
            print("  -", w)
        return 0

    if cmd == "reconcile":
        store = default_store(s)
        no_refresh = "--no-refresh" in args
        args = [a for a in args if a != "--no-refresh"]
        if args and args[0] == "--template":
            snap = build_snapshot(s, store, refresh=False)
            p = write_template(snap, DATA_DIR / "bux_figures.csv")
            print(f"Template written to {p}")
            print("Fill in quantity, average price and value per holding from the BUX app,")
            print(f"then run: python -m bux_analyser.cli reconcile {p}")
            return 0
        fig: Figures | None = None
        if args:
            fig = parse_figures(Path(args[0]).read_text(encoding="utf-8-sig"))
        snap = build_snapshot(s, store, refresh=not no_refresh)
        report = build_report(s, snap, store, fig)
        out = DATA_DIR / "reconciliation_report.txt"
        out.write_text(report, encoding="utf-8")
        print(report)
        print(f"(also saved to {out})")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
