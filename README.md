# bux-analyser

Personal investment intelligence platform for a BUX (NL) portfolio.

Status: research phase. Documents:

- [docs/feasibility-report.md](docs/feasibility-report.md) — original feasibility study, architecture and roadmap
- [docs/free-first-architecture.md](docs/free-first-architecture.md) — €0/month revision: free data-source map, provider abstraction, revised phases

## Phase 1 status

Built and tested (CSV-independent foundation):

- `bux_analyser/core/` — transaction model, average-cost ledger (positions, realized/unrealized P/L, dividends, fees, cash, deposits/withdrawals), valuation history, TWR, XIRR
- `bux_analyser/marketdata/` — provider abstraction with provenance on every value, Yahoo price provider, ECB FX provider (Frankfurter), cache-first SQLite store with health tracking and graceful degradation
- `bux_analyser/db.py` — SQLite schema (personal data + rebuildable cache), raw import rows always preserved

Waiting on the real BUX export before writing the importer and the dashboard.

### Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

All personal data stays in `data/` (git-ignored). Only public price/FX requests leave the machine.
