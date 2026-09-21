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

**Phase 3 — portfolio intelligence (done, pending live verification).** Scores,
recommendations, alerts, Monte Carlo, stress tests and allocation what-ifs.

**Phase 4 — advanced intelligence (done, pending live verification).** Company
fundamentals, ETF look-through, factor analysis, backtesting and a question layer.

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

- **Scores** — holdings ranked against each other on momentum and risk, each pillar
  built from named metrics, with the change since the last snapshot explained by
  subtraction rather than guessed. Valuation, growth and quality report as unavailable
  because they need fundamentals.
- **Recommendations** — deterministic rules over position size, risk share, score
  movement and drawdown. Every card names the rule that fired, what would stop it
  applying, and the fact that valuation is not assessed. No language model is involved.
- **Alerts** — editable threshold rules stored in the database, evaluated daily against
  price, portfolio and technical metrics, with per-rule cooldowns and a read/unread inbox.
- **Scenarios** — Monte Carlo by moving-block bootstrap, so streaks survive the
  resampling; the expected return is a stated choice rather than a silent extrapolation
  from history. Market and sector shocks passed through beta, the worst stretches the
  portfolio actually lived through, and equal-weight, minimum-variance and equal-risk
  alternatives compared on the history you hold.

- **Fundamentals** — official filings from SEC EDGAR, which publish the date each figure
  was filed, plus a best-effort source for European names that is labelled unverified and
  barred from point-in-time work. Valuation, growth and quality pillars score once
  statements exist; holdings without them stay unscored rather than assumed average.
- **ETF look-through** — issuer holdings files parsed by column meaning, so a fund is
  dissolved into what it actually holds. A fund whose file we lack, or whose file covers
  too little of it, keeps its own bucket instead of being guessed at.
- **Factor analysis** — the portfolio's returns regressed on market, size, value,
  profitability, investment and momentum, with a t-statistic per loading so a real tilt
  can be told from noise.
- **Backtesting** — walk-forward, with look-ahead prevented structurally: a rule is a
  function handed only the history up to its rebalance date. Costs charged on every
  trade, buy-and-hold alongside, and a permutation test that reruns the rule on shuffled
  signals to show what luck produces on the same data. Every result ships with the biases
  it cannot correct.
- **Question layer** — named functions over the database that answer in plain language,
  computed in Python, so they are correct by construction and need no model at all.
- **Briefing export** — assembles everything above into one markdown document wrapped in
  rules that forbid a model from calculating, estimating or recalling any number, and a
  section naming every real gap so a model cannot quietly fill one. Euro amounts are
  withheld by default: the briefing carries every weight, return and ratio, so the
  analysis is unaffected while the portfolio's size stays private. You copy it into
  whichever chat you trust; nothing is sent from the application.

Not built yet: news, and European filings via ESEF.

### Why there is no built-in chatbot

A model small enough to run locally on an ordinary machine is not good enough for this
work, and wiring the application to a cloud model would mean paying a subscription and
sending the portfolio automatically. The briefing export gets a strong model's judgement
with neither cost: you choose the model, you see exactly what you are sharing, and the
arithmetic has already been done by code that is tested.

### What it deliberately will not do

- Predict a price. Simulations show a range conditional on stated assumptions and say so.
- Present mean-variance "optimal" weights built on estimated returns. Minimum variance
  and equal risk are offered instead because neither needs a return forecast.
- Let a language model choose a score or a label.
- Claim a backtest it has not run: the recommendation rules are untested against outcomes,
  and the dashboard says that where they appear.
- Let a scraped figure masquerade as a filing, or use one in a point-in-time backtest.
- Read a backtest result without its biases attached.

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
  analytics/   returns, risk, indicators, portfolio assembly, scoring, simulation,
               fundamentals, factors, backtest
  ai/          the function catalogue an assistant may call, and the briefing export
  alerts.py    rule evaluation and the event inbox
  intelligence.py  scores, recommendations and alerts for a snapshot
  ui/          chart palette and builders
  service.py   snapshot assembly   cli.py   reconcile.py
app.py         Streamlit dashboard
```
