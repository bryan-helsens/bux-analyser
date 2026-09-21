# bux-analyser

Personal investment intelligence platform for a BUX (NL) portfolio.

Status: research phase. Documents:

- [docs/feasibility-report.md](docs/feasibility-report.md) — original feasibility study, architecture and roadmap
- [docs/free-first-architecture.md](docs/free-first-architecture.md) — €0/month revision: free data-source map, provider abstraction, revised phases

## Status

**Phase 1 — trustworthy ledger (done).** Working on a real 2026 BUX export: 465 rows
import to 394 transactions and the cash chain reconciles to the cent against BUX's own
running balance.

**Phase 2 — market intelligence (done, pending live verification).** Benchmarks,
exposure breakdowns, risk analytics and a tabbed dashboard.

### What it does

- **Import** — BUX transaction-history CSV: paired trade legs, fees, transaction taxes,
  dividends with gross/net/withholding, corporate actions, effective FX. Idempotent on
  source rows; raw rows retained. See [docs/bux-import-spec.md](docs/bux-import-spec.md).
- **Ledger** — positions on average cost, realised and unrealised P/L, dividends, fees,
  taxes, cash, deposits and withdrawals. Exact decimals throughout.
- **Performance** — value history, time-weighted return, XIRR, monthly return table,
  comparison against MSCI World, FTSE All-World, S&P 500, Nasdaq 100 and the AEX using
  EUR-listed accumulating UCITS ETFs as proxies.
- **Currency** — every position's return split exactly into share-price and exchange-rate
  components, so you can see which one actually moved your money.
- **Risk** — volatility, Sharpe, Sortino, max drawdown with dates and recovery, VaR and
  CVaR, beta, tracking error, up and down capture, correlation matrix, and each holding's
  share of portfolio volatility against its share of value.
- **Exposure** — sector, currency, country and asset type, with concentration measures.
  ETFs are shown as one bucket rather than given an invented sector split.
- **Provenance** — every external number carries its source, retrieval time and data date.
- **Degradation** — a missing price or a dead provider produces a stated gap, never a
  fabricated number.

Not built yet: ETF look-through, stock scoring, recommendations, alerts, Monte Carlo,
scenarios, backtesting and the local AI assistant.

### Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q
python -m bux_analyser.cli import /path/to/bux_export.csv   # or upload in the app
python -m bux_analyser.cli reconcile                        # refresh + verification report
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

If a symbol resolves to the wrong listing:
`python -m bux_analyser.cli set-ticker <ISIN> <YAHOO_SYMBOL>`, then refresh.

All personal data stays in `data/` (git-ignored). Only public symbols are sent to Yahoo
and the ECB rate feed.

### Layout

```
bux_analyser/
  importers/   BUX CSV parsing and idempotent persistence
  core/        transaction model, ledger, valuation, TWR, XIRR
  marketdata/  provider abstraction, Yahoo prices, ECB rates, cache-first store
  analytics/   returns, risk, portfolio assembly
  ui/          chart palette and builders
  service.py   snapshot assembly   cli.py   reconcile.py
app.py         Streamlit dashboard
```
