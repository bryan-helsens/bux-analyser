# bux-analyser

Personal investment intelligence platform for a BUX (NL) portfolio.

Status: research phase. Documents:

- [docs/feasibility-report.md](docs/feasibility-report.md) — original feasibility study, architecture and roadmap
- [docs/free-first-architecture.md](docs/free-first-architecture.md) — €0/month revision: free data-source map, provider abstraction, revised phases

## Phase 1 status

Working end to end on a real 2026 BUX export (465 rows → 394 transactions, cash reconciled to the cent against BUX's own running balance):

- `docs/bux-import-spec.md` — the export format as it actually is (two-row trades, fee/tax rows, dividend gross/net/tax, corporate-action pairs, effective FX)
- `bux_analyser/importers/` — parser + idempotent persistence (every source row hashed; raw rows kept)
- `bux_analyser/core/` — average-cost ledger, valuation history, TWR, XIRR
- `bux_analyser/marketdata/` — provider abstraction with provenance, Yahoo prices, ECB FX, cache-first SQLite store
- `bux_analyser/service.py` — snapshot for the dashboard; `bux_analyser/cli.py`; `app.py` (Streamlit)
- 21 deterministic tests on synthetic fixtures (your export is never committed)

### Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q
python -m bux_analyser.cli import /path/to/bux_export.csv   # or upload in the app
python -m bux_analyser.cli reconcile                        # refresh + full verification report
streamlit run app.py
```

To check the numbers against the BUX app:

```bash
python -m bux_analyser.cli reconcile --template     # writes data/bux_figures.csv
# fill in quantity / average price / value per holding from the app, then:
python -m bux_analyser.cli reconcile data/bux_figures.csv
```

Quantity and average cost come purely from the export and must match the app exactly.
Values depend on a live price, so small differences from delayed quotes and ECB reference
rates are reported but not treated as errors.

If a symbol resolves wrongly: `python -m bux_analyser.cli set-ticker <ISIN> <YAHOO_SYMBOL>` then refresh.

All personal data stays in `data/` (git-ignored). Only public symbols are sent to Yahoo/ECB.
