"""Command line: python -m bux_analyser.cli import <file> | refresh | status | set-ticker ISIN SYMBOL"""
from __future__ import annotations

import sys

from .db import make_engine, session_factory
from .importers.persist import import_bux_file
from .service import build_snapshot, default_store, refresh_market_data


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    s = session_factory(make_engine())()
    cmd, args = argv[0], argv[1:]
    if cmd == "import":
        r = import_bux_file(s, args[0])
        print(f"{r.filename}: {r.rows} rows, {r.new_rows} new, {r.new_txns} transactions" + (" (already imported)" if r.already_imported else ""))
        print("cash reconciliation:", "OK" if r.cash_ok else "MISMATCH")
        for m in r.cash_messages: print("  !", m)
        for w in r.warnings: print("  -", w)
        return 0
    if cmd == "refresh":
        store = default_store(s)
        for w in refresh_market_data(s, store): print("  -", w)
        for h in store.router.health(): print(f"{h.name}: {h.calls} calls, {h.failures} failures {h.last_error}")
        return 0
    if cmd == "set-ticker":
        default_store(s).set_manual_ticker(args[0], args[1]); print("ok"); return 0
    if cmd == "status":
        snap = build_snapshot(s, default_store(s))
        print(f"cash €{snap.cash:.2f}  securities {snap.securities_value}  total {snap.total_value}  MWR {snap.mwr}")
        for h in snap.holdings:
            print(f"{h.isin} {h.name[:26]:26} {h.quantity:>12} @ {h.avg_cost_local:>9.4f} {h.currency}  price {h.price} {h.price_currency}  value {h.value_base}  {h.issues}")
        for w in snap.warnings: print("  -", w)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
