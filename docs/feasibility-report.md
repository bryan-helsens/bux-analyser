# Personal Investment Intelligence Platform — Feasibility & Architecture Report

*Research date: 11 September 2026. Sources are cited inline; claims that could not be verified are marked **(unverified)**. Web fetches to `support.bux.com` and `bux.com` were blocked from this environment, so BUX-specific facts come from third-party importers' documentation and code, which is a reliable proxy for the export format but not for the app's current menu wording.*

## Executive Summary

**Feasibility: 74 / 100.** The project is realistic, but not in the shape described in the brief.

- **Getting your data out of BUX is solved and clean.** BUX Zero has a native transaction-history CSV export (emailed, max 3 requests/day). Its column schema is known from open-source importers: it includes ISIN-level asset IDs, quantities, prices, currencies, the FX rate applied, and gross/net/tax dividend amounts. The "export → upload → everything automatic" workflow is achievable. There is no official API and none is needed.
- **Portfolio tracking, allocation, performance and risk analytics are fully feasible** with standard Python libraries and one paid data feed. This is the part that will deliver most of the value, and it is the part that most hobby projects get subtly wrong (cost basis, FX, TWR vs MWR, look-through). Do this carefully and first.
- **Data is the binding constraint, not code.** European fundamentals with publication dates, historical index membership, and UCITS ETF constituent look-through are either paid (EODHD ≈ $60/month for fundamentals) or simply not available via a clean API. Every "intelligent" feature (scores, recommendations, backtests) inherits the quality of this data. Budget €20–80/month.
- **Prediction, in the sense the brief hopes for, is not realistic** and the report says so plainly (§8). What *is* realistic and useful: expected-return ranges from transparent fundamental assumptions, Monte Carlo on the portfolio, factor-based risk attribution, and stress tests. Build those; do not build ML price predictors.
- **Scoring and recommendations are feasible as a monitoring/triage layer**, explainable and rule-based. Their backtested alpha on a 10–30 stock personal portfolio will be close to zero and the product must be honest about that. The value is "look here first", not "this will outperform".
- **Roughly half of the wishlist is already solved well by existing tools** (Portfolio Performance for tracking, Morningstar for research/X-ray, Portfolio Visualizer for Monte Carlo). What does *not* exist is a EUR-first, BUX-fed, single-pane system with look-through risk, honest scenario analysis and an AI layer that cites its numbers. That gap is real and is the reason to build.
- **Recommended scope:** a local-first Python/FastAPI + SQLite analytics core with a Next.js (or initially Streamlit) dashboard, BUX CSV importer, EODHD + free sources for data, a rule-based scoring/recommendation layer with a proper walk-forward backtesting harness, and a tool-calling LLM assistant that can only report numbers it fetched from the database. Estimated effort for a competent single developer: ~4–6 months part-time to a genuinely useful V2.

---

## 1. BUX Data Acquisition

### What BUX offers today
| Channel | Status | Notes |
|---|---|---|
| Official public API | **None** for BUX Zero. The old BUX X CFD platform (renamed Stryk, then shut down) never had a retail API either. | [press.getbux.com](https://press.getbux.com/216421-bux-unveils-rebrand-of-derivatives-trading-platform-bux-x-to-stryk-reflecting-focus-on-the-needs-of-short-term-traders/), [financemagnates](https://www.financemagnates.com/forex/bux-quietly-shuts-down-its-cyprus-regulated-cfds-platform/) |
| **Transaction-history CSV export** | **Yes.** In-app: Account/Settings → Transaction history → pick date range & types → "Submit request" → CSV is **emailed** to your registered address. **Max 3 export requests per day.** | Documented by two independent importers: [Export-To-Ghostfolio](https://github.com/dickwolff/Export-To-Ghostfolio), [Portfolio Dividend Tracker](https://help-en.portfoliodividendtracker.com/article/237-import-your-bux-actions) |
| Annual statement (jaaroverzicht) | PDF, emailed each February; balances and dividend tax for Box 3. Useful as a reconciliation check, not as a data source. | support.bux.com snippet (page blocked; **partially verified**) |
| Monthly/quarterly statements, Excel export | **Not found (unverified)**. Check in-app. | |
| Open banking / PSD2 aggregators (Tink, Salt Edge, Powens, Plaid) | **Not applicable.** PSD2 covers payment accounts only; brokerage accounts are out of scope. No aggregator lists BUX. | |
| Unofficial reverse-engineered API (`orsinium-labs/bux`, `libbux`) | Exists; **excluded by your constraints and by prudence**: undocumented, breaks silently, ToS position unclear (Client Agreement reportedly restricts non-official order placement; read access unaddressed). | [github](https://github.com/orsinium-labs/bux) |
| GDPR Art. 15/20 data request | Legal right; no reports of anyone doing this with BUX; expect weeks and an unknown format. Fallback only. | |
| Screenshot/OCR, manual entry, copy-paste | Possible but strictly inferior to the CSV; keep manual entry as an *editing* feature for corrections, not as an import path. | |

Context: ABN AMRO completed its acquisition of BUX in July 2024 ([press.getbux.com](https://press.getbux.com/239027-abn-amro-completes-acquisition-of-bux-bux-becomes-a-subsidiary/)). No source describes changes to the export function since, but app menus may have moved; Portfolio Dividend Tracker maintains a "BUX import changelog", implying the CSV format has changed at least once. The importer must therefore be **schema-tolerant** (map columns by name, warn on unknown columns, keep the raw file).

### The BUX CSV schema (from the open-source converter code)
Columns: `assetId, assetName, assetCurrency, assetQuantity, assetPrice, transactionType, transactionAmount, transactionCurrency, transactionTimeCet, exchangeRate, transferType, cashBalanceAmount, profitAndLossAmount, dividendGrossAmount, dividendNetAmount, dividendTaxAmount`.
Transaction types seen: buy, sell, dividend, interest, fee, deposit, withdrawal, promotional, corrections, cash debit, "general corporate action". Quirks: GBX-quoted UK stocks; buys with positive amount are actually sells; timestamps in CET. `assetId` is the instrument identifier (ISIN-based; the exact form should be confirmed from your file). **This is a rich export**: it carries the broker's own FX rate and dividend tax, which most brokers omit. **Not carried**: explicit per-order commission as a column (fees appear as separate `fee` rows) and the BUX-side sector/name metadata beyond `assetName`.

### Ranking
1. **Best — native CSV export, uploaded to our importer.** Free, legitimate, contains everything needed. Cadence: after each trade or weekly; 3/day limit is irrelevant for a long-term investor. Idempotent re-import (row hashing) means you can always export "all history" and re-upload.
2. **Good — CSV export via an existing tracker as intermediate** (Export-To-Ghostfolio → Ghostfolio, or Portfolio Performance's CSV import) if you want a second, independently-maintained ledger for reconciliation.
3. **Backup — GDPR data request** if the export ever disappears; **manual entry/correction UI** for edge cases (corporate actions BUX records oddly).
4. **Not recommended — unofficial API, scraping, OCR of screenshots, PSD2 aggregators.**

**How close to "export → upload → automatic": very close.** The only manual steps are triggering the export in the app and dragging the emailed CSV into the app (or pointing the importer at a mail folder / IMAP rule, which would make it fully automatic but is optional polish). Everything after upload — ISIN → ticker matching, price/fundamental retrieval, portfolio calculation, dashboard — is automatable.

**Please confirm from the app (only you can):** current menu path, whether the 3/day limit still applies, and send one anonymised export (a few rows with amounts changed is fine) so the importer is built against the real 2026 format.

---

## 2. Existing Platforms

Short verdicts (details and prices in the agents' tables; prices checked Sept 2026, some via secondary sites):

| Platform | Role | Verdict for you |
|---|---|---|
| **Portfolio Performance** (free, desktop, OSS) | Tracking | Gold standard for correct TWR/IRR, FX, dividends, fees. No BUX importer, but generic CSV works. **Use it as the reference implementation to validate our numbers against.** |
| **Ghostfolio** (OSS, self-host) | Tracking | Modern, API-first, has a community BUX converter. **Closest existing thing to our MVP tracker.** Honest question: fork it or build ours? See §21. |
| Parqet, getquin, Sharesight, Delta, Snowball, Finary | Tracking (SaaS) | Fine trackers; none confirm BUX; all cloud-hosted (your data leaves your machine). Not needed. |
| **Portseido**, Portfolio Dividend Tracker | Tracking | The only two that *explicitly* advertise BUX CSV import. Worth a 10-minute look to see how they map the file. |
| **Morningstar Investor** ($249/yr) | Research + risk | Best EU equity/fund coverage; **X-Ray** is best-in-class ETF look-through; Fair Value/Moat has a long published methodology. **The one paid research product worth considering alongside our tool.** |
| **Koyfin** (free / $39 / $79) | Research terminal | Best data breadth for EU mid-caps and dashboards; no scoring, no Monte Carlo. Good for manual deep-dives. |
| Simply Wall St, Stock Rover, Seeking Alpha Quant | Research + scores | Scores are marketing-backtested only; no independent evidence. EU small/mid-cap depth thinner. Do not pay for these to get "scores". |
| Finviz | Screener | **US-only**, irrelevant for the EU half. |
| TradingView | Charting | Unbeatable charts and free; not a portfolio tool. Use its free tier for charting rather than rebuilding; embed Lightweight Charts (same vendor, OSS) in our app. |
| Yahoo Finance / Google Finance | Quotes | Free, shallow, inconsistent data quality. Backup data source at most. |
| **Portfolio Visualizer** (free capped / $30/mo) | Risk & backtest | Best dedicated Monte Carlo/factor/backtest tool, but US-fund-centric; UCITS tickers poorly supported. Use as a *sanity check* for our Monte Carlo/factor code. |
| Interactive Brokers PortfolioAnalyst | — | Excellent, but account-linked; irrelevant unless you change broker. |

**Already solved well elsewhere (don't rebuild for their own sake):** charting (TradingView), ETF X-ray with analyst ratings (Morningstar), generic multi-currency tracking (Portfolio Performance), US screening (StockAnalysis/Finviz).

**Genuinely missing for a EUR/BUX investor** (confirmed by the survey): (a) any BUX-native pipeline; (b) one place combining EUR TWR/MWR + benchmark + look-through exposure + risk decomposition + Monte Carlo + scenarios; (c) EU and US fundamentals of equal depth in one place at hobby prices; (d) an AI layer that answers portfolio questions from *your* numbers with citations; (e) any independently validated quant score.

**Does building make sense?** Yes, for (a), (b), (d) and for the learning/control value. It does **not** make sense to rebuild charting, filings search, or analyst-grade fundamental research; link out or subscribe for those.

---

## 3. Financial Data Providers

Verified Sept 2026 (official pricing pages unless noted). The user's situation drives the choice: **EU + US equities and UCITS ETFs, ISIN-keyed, EUR base, personal use.**

| Provider | Free tier | Paid entry | EU prices | EU fundamentals | US fundamentals | Estimates | ISIN | Note |
|---|---|---|---|---|---|---|---|---|
| **EODHD** | 20 calls/day | **$19.99/mo All-World EOD**; $59.99/mo Fundamentals; $99.99 all-in | **Strong** (18 EU markets, `.AS/.XETRA/.PA/.LSE`) | Yes (paid) | Yes (paid) | Yes | Yes | Best single fit; also historical index constituents (needed for backtests) |
| yfinance (Yahoo, unofficial) | Unlimited | — | Partial | Spotty | Basic | Limited | No | Breaks periodically; ToS grey; backup only |
| Alpha Vantage | 25 req/**day** | $49.99/mo | Patchy | Weak | Good | Some | No | Free tier no longer usable |
| Finnhub | 60/min, intl EOD only | $12–100/mo | EOD only | Paid add-on | Good | Yes | Paid | Decent; EU fundamentals cost **(unverified)** |
| FMP | 250/day, **US-only** | ~$20–30/mo | No (free) | Paid | Good | Paid | Paid | Free tier gutted 2024/25 |
| Polygon → **Massive** | 5/min | $29/mo | **US-only** | No | — | No | No | Irrelevant for EU |
| Twelve Data | 8/min, 800/day (credits) | ~$29/mo | Yes (90+ exch.) | Depth **(unverified)** | Yes | Paid | ? | Second candidate; trial before committing |
| Tiingo | US-focused | Fundamentals now sales-only | Weak | Thin | Good | ? | ? | Not for EU |
| Marketstack | 100/**month** | $8.99 | Yes | No | No | No | ? | Prices only, too thin free |
| Nasdaq Data Link | dataset-specific | varies | Legacy | — | — | — | — | Stagnant |
| **SEC EDGAR** (companyfacts XBRL) | Unlimited (10/s) | free | — | — | **Excellent, authoritative, with filing dates** | No | No | Use for US point-in-time fundamentals |
| **Frankfurter** (ECB rates) | Unlimited | free | FX | — | — | — | — | All FX needs |
| **OpenFIGI** | Free | free | — | — | — | — | ISIN→FIGI/ticker | Mapping layer |
| Stooq | free | — | Yes | No | No | No | No | Now CAPTCHA-keyed; fragile |
| justETF | no API | — | — | — | — | — | — | **No official ETF-holdings API anywhere**; scraping = ToS risk |
| Marketaux / NewsAPI / GDELT | ~100/day / 100/day / unlimited | $29 / $449 / free | news | | | | | Personal-use free tiers fine |
| FRED / ECB Data Portal / Eurostat | free | free | macro | | | | | Enough for macro context |
| Kenneth French / AQR factor data | free | free | factor returns (US, Europe, Global) | | | | | For factor regressions |

**Key findings**
- (a) **No confirmed source gives full financial statements for European companies below ~€40/month.** EODHD Fundamentals ($59.99/mo, includes EOD) is the credible option. Twelve Data may be cheaper but EU depth is unverified. Finnhub's EU fundamentals price is bundle-dependent.
- (b) EOD prices for EU stocks and UCITS ETFs by ISIN: EODHD (best), Twelve Data, yfinance (backup).
- (c) US-only: FMP free, Polygon/Massive, EDGAR, FRED.
- (d) **ETF look-through is an unsolved data problem.** No official API exposes UCITS ETF holdings. Options: EODHD's fundamentals feed includes ETF top holdings, sector and country weights for many UCITS ETFs **(coverage unverified; test with your ISINs)**; issuer factsheets/holdings files (iShares, Vanguard, Xtrackers publish daily holdings CSVs on their websites, downloadable per ETF) — a small per-issuer downloader is legitimate and robust; manual entry from the KIID as fallback.
- **Point-in-time fundamentals** (publication dates) exist for US via EDGAR (free) and partially via EODHD; for EU they are generally absent → backtests must apply a conservative reporting lag (§13).

### Recommendation
| Set | Components | Cost |
|---|---|---|
| **Start (Phase 1–2)** | EODHD All-World EOD ($19.99) + EDGAR (US fundamentals) + Frankfurter + OpenFIGI + Marketaux free + issuer ETF holdings files + yfinance as fallback | **≈ €20/month** |
| **Quality (Phase 2+)** | EODHD Fundamentals ($59.99, includes EOD) + EDGAR cross-check + Frankfurter + OpenFIGI + Marketaux free/paid | **≈ €55–85/month** |

Rationale: one primary vendor with strong EU coverage, ISIN support, index-constituent history and an actively maintained API; free authoritative sources where they exist; no dependence on scraping. Wrap every provider behind a `MarketDataProvider` interface with a cache so vendors can be swapped, and record `source` + `fetched_at` on every row.

## 4. Portfolio Data Model

Design principle: **transactions are the source of truth; everything else is derived and recomputable.** Never store "current portfolio" as editable state. Positions, P/L, weights and history are views over the transaction ledger plus market data. This is what Portfolio Performance, Ghostfolio and every accounting-grade tracker do, and it is what makes re-imports, corrections and audits painless.

### Core tables (PostgreSQL or SQLite, same schema)

```sql
-- Reference data (public, cacheable, refreshed by jobs)
security(
  id PK, isin UNIQUE, name, asset_type ENUM('stock','etf','cash','other'),
  primary_ticker, primary_exchange (MIC), trading_currency,
  sector, industry, country_of_domicile, market_cap_bucket,
  data_provider_ids JSONB,      -- {"eodhd":"ASML.AS","yahoo":"ASML.AS","openfigi":"BBG..."}
  is_active, last_refreshed
)
security_listing(id PK, security_id FK, ticker, exchange MIC, currency)   -- multi-listing support
price_eod(security_id FK, date, open, high, low, close, adj_close, volume, currency, PK(security_id,date))
price_intraday_cache(security_id, ts, price, currency)                     -- optional, short TTL
fx_rate(base CHAR(3), quote CHAR(3), date, rate, source, PK(base,quote,date))
fundamental_snapshot(security_id, period_end DATE, period_type ENUM('FY','Q','TTM'),
                     as_reported_at DATE,   -- <- publication date: essential for point-in-time backtests
                     revenue, gross_profit, ebit, ebitda, net_income, eps_diluted,
                     operating_cf, capex, fcf, cash, total_debt, equity, shares_out,
                     source, raw JSONB, PK(security_id, period_end, period_type))
estimate_snapshot(security_id, as_of DATE, fy_offset INT, eps_mean, revenue_mean, n_analysts, source)
etf_holding(etf_security_id, as_of, constituent_isin, weight, sector, country)  -- for look-through
corporate_action(security_id, date, type ENUM('split','dividend','spinoff','rename','isin_change'), ratio, amount, currency)

-- Personal data (private, local)
account(id PK, name, broker 'BUX', base_currency 'EUR')
transaction(
  id PK, account_id FK, security_id FK NULL,
  type ENUM('buy','sell','dividend','fee','tax','deposit','withdrawal','interest','split','transfer_in','transfer_out'),
  trade_date, settle_date, quantity NUMERIC(20,8), price NUMERIC(20,8), price_currency,
  gross_amount, fee, tax, net_amount, amount_currency,
  fx_rate_to_base NUMERIC(20,10),       -- the broker's actual rate, if known; else ECB rate of the day
  note, import_batch_id FK, source_row_hash UNIQUE   -- idempotent re-import
)
import_batch(id PK, filename, file_hash, imported_at, row_count, status, warnings JSONB)
watchlist_item(security_id, added_at, note)
alert_rule(id, scope ENUM('security','portfolio'), security_id NULL, metric, operator, threshold, cooldown_hours, enabled)
alert_event(rule_id, fired_at, value, message, acknowledged)

-- Derived / cached analytics (fully rebuildable; can be wiped)
portfolio_daily(account_id, date, value_base, invested_base, cash_base, twr_index, mwr_ytd, PK(account_id,date))
position_daily(account_id, security_id, date, qty, price_local, fx, value_base, cost_basis_base, weight)
score_snapshot(security_id, as_of, model_version, valuation, growth, quality, momentum, risk, overall, components JSONB)
recommendation_snapshot(security_id, as_of, model_version, label, confidence, reasons JSONB, invalidators JSONB)
```

Notes:
- Store money as `NUMERIC`, never float. Store quantities to 8 decimals (BUX supports fractional shares).
- `as_reported_at` on fundamentals is non-negotiable if you ever want an honest backtest (see §13).
- `source_row_hash` makes re-uploading the same BUX export a no-op instead of creating duplicates.
- Cost basis method: keep it configurable (average cost is what BUX shows; FIFO is what Dutch reporting conventions usually assume; Box 3 taxation does not actually care about realized gains, so average cost is fine for display).
- Derived metrics per position: quantity, average cost (base and local currency), current value, unrealized P/L split into local-currency P/L and FX P/L (see §16), realized P/L, dividends received, fees paid, weight, holding-period return, contribution to portfolio return, contribution to portfolio risk.


---

## 16. Currency Handling (put here because the data model depends on it)

- Base currency EUR. Every transaction stores its native currency amount **and** the FX rate applied on that date (broker rate if the export includes it, otherwise ECB reference rate of the trade date).
- Daily valuation: `value_EUR = qty × price_local × fx(local→EUR, date)`.
- **Return decomposition** (for a EUR investor holding a USD stock):
  - Local return: `r_local = P1/P0 − 1`
  - Currency return: `r_fx = FX1/FX0 − 1`
  - Total EUR return: `(1+r_local)(1+r_fx) − 1 ≈ r_local + r_fx + r_local·r_fx`
  - Show all three per position and aggregated for the portfolio ("Your +8.1% this year was +11.4% from stock prices and −3.0% from the dollar").
- ETFs: a UCITS ETF quoted in EUR on Xetra can still carry USD exposure through its holdings. Currency exposure must be computed on **look-through** (ETF holdings' currencies), not on the ETF's quote currency. Same for country/sector exposure. This needs ETF constituent data (§3).
- Only ECB reference rates are needed for analytics; a free source is enough.


---

## 19. Technical Architecture

Recommended stack (boring, well-supported, single developer friendly):

| Layer | Choice | Why |
|---|---|---|
| Backend | **Python 3.12 + FastAPI** | The entire analytics stack (pandas, numpy, scipy, statsmodels, scikit-learn, PyPortfolioOpt, arch/GARCH, vectorbt) is Python; one language for jobs + API |
| Jobs | APScheduler (in-process) or a `cron` container | Daily EOD refresh, weekly fundamentals refresh, alert evaluation; no Celery/Redis needed |
| Database | **SQLite** for MVP (via SQLAlchemy 2 + Alembic), **PostgreSQL** if/when concurrent jobs or >5 years of a 1,000-name universe make it slow | Same ORM, migration path is trivial; DuckDB as an analytics sidecar if the universe grows |
| Frontend | **Next.js (React) + TypeScript + Tailwind + shadcn/ui** | Best charting ecosystem, dashboard-quality UI, static export possible |
| Charts | **TradingView Lightweight Charts** (price/candles), **ECharts** or **Plotly.js** (allocation sunburst/treemap, fan charts, heatmaps, correlation matrices), **Recharts** for small KPI sparklines | Lightweight Charts is the standard for financial charts; ECharts handles large interactive charts well |
| Deployment | Docker Compose (api, web, optional postgres, optional ollama) | One command on laptop or NAS |
| LLM | Anthropic/OpenAI API via a thin tool-calling layer, or Ollama locally | Swappable |

Alternative if you want to minimise frontend work: **Streamlit** or **Dash** (pure Python). Faster to build, clearly less "professional dashboard". A reasonable path is Streamlit for Phase 1 to validate the analytics, then Next.js in Phase 3 when the numbers are trusted. Choosing the polished frontend first is the classic way personal finance projects die before the analytics work.

Module layout:
```
app/
  importers/     bux_csv.py, bux_pdf.py, generic_csv.py, portfolio_performance_csv.py
  marketdata/    provider interface + eodhd.py, yahoo.py, fx_ecb.py, openfigi.py, edgar.py
  core/          ledger.py (positions/cost basis), valuation.py, performance.py (TWR/MWR), fx.py
  analytics/     risk.py, factors.py, scores.py, recommend.py, montecarlo.py, whatif.py, backtest/
  alerts/        rules.py, evaluate.py, notify.py
  ai/            tools.py (functions exposed to LLM), prompts.py, client.py
  api/           FastAPI routers
web/             Next.js
```


---

## 5. & 6. Dashboard and Stock Page — UX notes (brief)
- Overview strip (value, day Δ, total return, TWR YTD, vs benchmark, cash, best/worst), then allocation (treemap with sector/country/currency toggles on look-through), then performance (value vs invested vs benchmark; period-return bars; monthly heatmap), then holdings table with sortable score/risk columns, then alerts inbox and "what changed since yesterday" feed.
- Stock page: header with local vs EUR return; tabs Overview / Valuation (metric vs own history band + sector) / Growth / Quality / Risk / Scores (with diff history) / Scenarios / News. Each number shows its source and as-of date on hover.
- Design principle: show the *comparison* (vs own history, vs sector, vs benchmark) rather than the raw number; a P/E of 28 means nothing alone.

---

## 8. Prediction & Forecasting — what is realistic

Blunt assessment first, then what to build.

**What the evidence says**
- Short-horizon (days–months) price prediction from technical indicators or from price history with ML has, in every serious out-of-sample study, near-zero edge after costs for a retail investor. The academic momentum anomaly (12-1 month cross-sectional momentum) is real but is a *portfolio-level* factor with large crashes, not a per-stock forecasting tool.
- Fundamental-driven expected returns have a modest but genuine signal at 3–10 year horizons: starting valuation (earnings yield, CAPE, FCF yield) explains a meaningful share of subsequent long-horizon return variance at the index level; at the single-stock level the signal is much weaker and noisy.
- Analyst consensus estimates are systematically over-optimistic for years 2+ and revisions (direction of change) carry more information than levels.
- LLMs do not predict prices. They are useful for reading, summarising and structuring text (filings, news), not for forecasting.

**Therefore: build "expected-return ranges under stated assumptions", never "price targets".**

### 8.1 Fundamental expected-return model (per stock, 5-year horizon) — recommended
A transparent decomposition (Bogle/Grinold style):

```
E[annual return] ≈ dividend yield
                 + expected EPS (or FCF/share) growth
                 + annualised multiple change  ((target_multiple / current_multiple)^(1/5) − 1)
```
with growth drawn as a distribution (e.g. triangular between "analyst consensus haircut by 30%", "trailing 5-yr CAGR", "sector median") and the target multiple drawn from the stock's own 10-year valuation range and the sector's range. Run 10,000 draws → a fan chart of 5-year outcomes. This is explainable, every input is visible and editable, and it is how most professional long-only shops actually think.

### 8.2 Monte Carlo for the portfolio — recommended
- Bootstrap historical joint daily/monthly returns of the holdings (block bootstrap, e.g. 20-day blocks, to preserve autocorrelation and preserve cross-asset correlation automatically). Do **not** use plain Gaussian GBM with historical mean; it understates tails and the historical mean is the least reliable estimate you have.
- Show: distribution of terminal wealth at 1/3/5/10 years, probability of drawdown > X, 5th/25th/50th/75th/95th percentile paths, with and without monthly contributions.
- Crucial honesty feature: let the user override expected returns (e.g. set all expected excess returns to 0 or to a conservative equity premium of 3–5%) so the result reflects **assumptions**, not history extrapolation. Label the chart "conditional on assumptions shown here".

### 8.3 Technical analysis — include as *description*, not prediction
Compute SMA 50/200, RSI 14, MACD, 12-1 momentum, 20-day realised volatility, distance from 52-week high, drawdown. Use them for (a) alerts, (b) the Momentum score, (c) the "what changed" narrative. Do not build trading signals on them; backtests will show they do not beat buy-and-hold after costs on a 10–30 stock portfolio.

### 8.4 Time-series / ML price models — do NOT build in MVP; optional V3 experiment
ARIMA/GARCH are fine for **volatility** forecasting (GARCH(1,1) genuinely improves risk estimates for VaR), not for returns. Gradient boosting / LSTMs on prices are a well-documented trap: they fit noise, and a correct walk-forward backtest almost always shows Sharpe ≈ 0 after costs. If you want to experiment, do it in V3 inside the backtesting harness with the pre-registered hypothesis "beats equal-weight buy-and-hold after 0.2% cost per trade", and expect it to fail.

### 8.5 Factor models — useful, cheap, and explanatory
Regress each holding's and the portfolio's excess returns on Fama-French 5 factors + momentum (Kenneth French data library is free; a European set exists too). Output: "your portfolio behaves like 1.1× market, tilted to growth (−0.3 HML), large cap, with a momentum tilt". This is *descriptive* and genuinely useful for risk understanding. It also feeds expected-return estimates via factor premia if desired.


---

## 9. Stock Scoring System

How professional quant systems do it (and what to copy):
1. Choose a small number of **pillars** with a clear economic rationale.
2. Inside each pillar, pick 3–6 metrics with **published evidence** (e.g. Piotroski F-score components, Novy-Marx gross profitability, Sloan accruals, Altman Z, 12-1 momentum, low-vol).
3. Transform each metric to a **cross-sectional percentile rank** (0–100) within a peer group (sector × region), winsorised at 1/99. Never use raw z-scores across sectors: a bank's P/B and a software company's P/B are not comparable.
4. Average ranks within a pillar (equal weight unless you have backtested evidence otherwise); average pillars into an overall score.
5. Version the model; store the components with every snapshot so the "why did it change" diff is a mechanical subtraction, not an LLM guess.

Proposed pillars:

| Pillar | Metrics (percentile-ranked vs sector/region peers) | Direction |
|---|---|---|
| Valuation | EV/EBIT, P/E (fwd), FCF yield, P/S vs own 10-yr median, PEG | cheaper = higher |
| Growth | 3-yr revenue CAGR, 3-yr EPS CAGR, fwd EPS growth (consensus), FCF growth, estimate revisions (3m) | higher = higher |
| Quality | ROIC, gross margin, gross-profit/assets, FCF conversion (FCF/NI), net debt/EBITDA (inverse), accruals (inverse), Piotroski F | better = higher |
| Momentum | 12-1 price momentum, 6-month, price vs 200d SMA, EPS revision momentum | stronger = higher |
| Risk (low = good) | 1-yr realised vol, beta, max drawdown 3y, net debt/equity, Altman Z (inverse), single-country/customer concentration flag | lower risk = higher |
| Overall | mean of pillars (default equal; user-adjustable weights) | |

The user's proposed "Fundamental score" is redundant with Growth + Quality; drop it.

Score change explanation: `Δoverall = Σ pillar_weight × Δpillar`, `Δpillar = Σ Δmetric_rank / n`; list the top-3 contributors by absolute change with the underlying values ("FCF yield rank 71 → 44 because price +18% and TTM FCF −6%"). Also report **data staleness** ("fundamentals as of FY2025, 9 months old") because a score built on stale numbers is a common silent failure.

Important caveat to display: cross-sectional ranks require a **universe**. For ~30 holdings you cannot rank against yourselves; you need a peer universe (e.g. STOXX 600 + S&P 500 ≈ 1,100 names) with fundamentals for all of them. This drives the data-provider choice (§3) and cost.


---

## 10. Portfolio Optimisation & Risk

Build (all standard, all in `numpy/pandas/scipy`, no exotic libraries needed):
- Return statistics: TWR (daily-linked), MWR/XIRR, CAGR, annualised vol, Sharpe (vs ECB deposit rate / €STR), Sortino, max drawdown & duration, Calmar, rolling 1-yr versions of all.
- Benchmark comparison: MSCI World (via IWDA/URTH), S&P 500 (VUSA), AEX, Nasdaq-100, plus a custom blend (e.g. 70/30). Beta, tracking error, information ratio, up/down capture.
- Risk decomposition: covariance from 1–3 years of daily returns with **Ledoit-Wolf shrinkage** (sklearn), marginal contribution to risk per holding, percent risk contribution (this is the answer to "which positions contribute most to volatility"), correlation matrix with hierarchical clustering ordering.
- Concentration: HHI, top-N weight, effective number of positions, look-through sector/country/currency exposure incl. ETFs.
- VaR / CVaR: historical (1-day and 1-month, 95/99%), plus GARCH-scaled parametric as a cross-check. State plainly that VaR is a model output.
- Efficient frontier / optimisers: mean-variance, **minimum variance**, **risk parity** (equal risk contribution), max diversification, and hierarchical risk parity (HRP). Use `PyPortfolioOpt` or `Riskfolio-Lib` — both mature.
- **Blunt caveat to design in**: mean-variance optimisation is an "error maximiser": tiny changes in expected returns swing weights wildly. Never show an MVO "optimal" portfolio built from historical mean returns as advice. Show min-variance, risk-parity and HRP (which do not need return forecasts) as *alternatives*, and constrain turnover. This is where many hobby tools actively mislead.
- Issue detection rules (deterministic, threshold-configurable): position > 15%, top-2 > 35%, sector > 35%, single country > 70% (excluding global ETFs' look-through), pairwise correlation > 0.85 between two positions each > 5%, portfolio beta > 1.3, cash drag > 15% for > 6 months, position with negative 3-yr fundamental trend and weight > 5%.


---

## 11. Recommendation Engine

Rule-based, fully explainable, no LLM in the decision path. The LLM may **phrase** the explanation, never choose the label.

```
REVIEW      if score_overall dropped ≥ 12 pts in 90d, OR quality pillar < 30, OR estimate revisions ≤ −10%,
            OR any fundamental alert fired (margin/debt/guidance), OR data stale > 15 months
REDUCE      if weight > user_max_weight, OR pct_risk_contribution > 30%, OR (valuation < 20 AND weight > 8%)
WATCH       if score_overall ≥ 65 AND not held (watchlist), OR held with momentum < 25 but quality ≥ 60
OPPORTUNITY if valuation ≥ 70 AND quality ≥ 55 AND estimate revisions ≥ 0 AND momentum ≥ 30  (avoid pure value traps)
HOLD        otherwise
```
Every card: label, top reasons with metric values, risks, **confidence** (derived mechanically from: data freshness, number of analysts, distance from thresholds, agreement across pillars), "what would invalidate this" (the thresholds that, if crossed, flip the label), and data date. Confidence ≠ probability of being right; say so.

Honest framing: this is a *monitoring* engine that triages attention ("look here first"), not an alpha engine. A backtest (§13) will tell you whether OPPORTUNITY/REDUCE labels historically added value; the expectation is small-to-none and the honest product will say that.


---

## 13. Backtesting — how to know whether any of this works

Requirements (non-negotiable if the results are to mean anything):
1. **Point-in-time data**: fundamentals keyed by *publication date* (`as_reported_at`), not fiscal period end. Using FY2024 numbers from 1 Jan 2025 when they were published in March is look-ahead bias and will inflate every fundamental strategy. Free/cheap providers mostly do **not** give publication dates for European stocks; this is the single biggest data risk of the project. Mitigation: assume a conservative lag (annual: +90 days; quarterly: +45 days) and say so.
2. **Survivorship-free universe**: use historical index constituent lists (delisted names included). EODHD has this; most cheap providers do not. Without it, expect a 1–3%/yr upward bias.
3. **Walk-forward** evaluation: all ranks/parameters computed with data available at rebalance date only; expanding or rolling windows; no parameter tuned on the test period.
4. **Costs**: 0.1–0.3% per trade + spread; BUX charges per-order fees in EUR, model them explicitly. Monthly/quarterly rebalance only.
5. **Benchmarks**: buy-and-hold of the same starting portfolio, equal-weight universe, MSCI World, and a *random-rank* strategy (permutation test: run 500 backtests with shuffled scores to get the null distribution of Sharpe; your strategy must beat the 95th percentile to be credible).
6. **Metrics**: CAGR, vol, Sharpe, Sortino, max DD, Calmar, turnover, hit rate, alpha/beta vs benchmark with t-stats, and **deflated Sharpe ratio** (Bailey & López de Prado) to account for the number of variants you tried.
7. **Multiple-testing discipline**: log every strategy variant you run; the more you try, the higher the bar.

Engine: `vectorbt` (fast, vectorised, good for signals) or a small custom pandas engine (recommended: ~500 lines, fully transparent, easier to audit than a framework). `bt` and `zipline-reloaded` are alternatives; `backtrader` is unmaintained. Keep the engine simple: monthly rebalance, long-only, weights from a function `score → weights`.

Expected finding, stated in advance: a quality+value+momentum composite rebalanced quarterly on a broad universe typically shows a modest gross improvement in Sharpe vs the cap-weighted index over 15–20 years, with multi-year underperformance stretches and results sensitive to universe and costs. Applied to a 10–30 stock personal portfolio, noise dominates. The product should present backtests as *evidence about the method*, not a promise about the portfolio.


---

## 14. What-If Simulator

All scenarios are re-runs of the same valuation + risk pipeline on a modified transaction ledger or weight vector, so the simulator is cheap once the pipeline exists:
- Contribution plans (€300/month into X): deterministic under assumed return, plus Monte Carlo fan.
- Weight changes / sells / adds: recompute exposure, risk contribution, factor loadings, historical stats of the hypothetical portfolio (with the honest label "what this mix *would have* done").
- Shocks ("tech −40%", "market −30%"): apply factor-based stress: each holding's shocked return = beta_to_factor × shock (+ idiosyncratic 0), using look-through for ETFs. Also replay historical episodes (2008, Mar-2020, 2022) on current weights.
- "Best historical risk-adjusted allocation": show min-variance / risk-parity / HRP alternatives with turnover and the caveat. Never label it "optimal".
- Comparison view: side-by-side table + overlaid fan charts for up to 4 scenarios.


---

## 12. Alerts

Simple design: a daily job (after EOD data refresh) evaluates all `alert_rule`s and writes `alert_event`s; delivery via in-app inbox plus optional email/Telegram/ntfy push. All thresholds are per-rule config with sensible defaults. Categories map directly to metrics already computed: price moves, valuation percentile vs own history, fundamental deltas at each new statement, portfolio concentration/drawdown, technical crossovers, earnings-date proximity, and news hits by ticker. Intraday alerts require intraday data and a running process; skip in MVP (EOD is sufficient for a long-term investor and far cheaper).


---

## 15. News & External Information

- Company filings: SEC EDGAR full-text and XBRL (free, US). For EU: company IR RSS feeds and national registers (AFM register for NL, Euronext press releases) are fragmented; a news API with company tagging is the pragmatic route.
- Show source, timestamp, ticker tag, headline; store raw links; summarise with the LLM only on demand and always with the link. Earnings calendar from the data provider drives the "earnings in 5 days" alert.
- Keep scope modest: news is the feature with the worst signal-to-noise and the highest maintenance (APIs churn, feeds break). Free-tier news APIs are enough for a personal watchlist.


---

## 7. AI-Assisted Research — architecture that keeps it honest

- The LLM is a **tool-using analyst over your database**, not an oracle. Give it functions: `get_position_stats`, `get_valuation_history`, `get_risk_decomposition`, `get_score_diff`, `get_recent_news`, `get_fundamentals`. Every answer must cite which function outputs it used; render those as a "data used" panel under the answer.
- Answer template enforced by system prompt: **Facts** (from DB) → **Calculations** (which metric, which window) → **Interpretation** (marked as AI opinion) → **Uncertainty / what we cannot tell from the data**. Refuse to give price targets.
- Typical questions map cleanly: "why is X performing poorly" → return decomposition (local vs FX), factor attribution, score diff, news list. "Am I overexposed to tech" → look-through sector exposure vs benchmark weight. "Which holdings contribute most to volatility" → pct risk contribution table. None of these need the LLM to *know* anything; they need it to call the right functions and write two paragraphs.
- Privacy: portfolio data must go to the model to answer portfolio questions. Options: (1) local model via Ollama (Llama/Qwen ~8–14B is adequate for this templated task; zero data leaves the machine), (2) cloud API with the portfolio expressed as **weights and metrics only** (no account IDs, no absolute euro amounts — normalise to 100), which is pseudonymous enough for a personal tool, (3) cloud API with full data — acceptable if you accept the provider's data-retention terms. Recommend (2) by default with (1) as an option.
- Cost: a few cents per question with a mid-tier cloud model; negligible.


---

## 18. Privacy & Security

Threat model: personal financial data on your own machine; attackers are (a) data-provider/AI vendors seeing your holdings, (b) a lost/stolen laptop, (c) an exposed web port. Design:
- **Local-first**: everything runs on your machine (or a home server / NAS) in Docker. No cloud database. Only outbound calls are to market-data APIs (public tickers only — they learn which tickers you look at, not what you own, and even that can be blurred by also fetching a peer universe) and optionally the LLM API (see §7).
- Encryption at rest: SQLite with full-disk encryption (FileVault/BitLocker/LUKS) is adequate; SQLCipher or PostgreSQL on an encrypted volume if you want belt-and-braces. Backups: encrypted (age/restic) to a location of your choice; the whole personal dataset is tiny (KBs).
- Auth: if only reachable on localhost, a single password is enough. If exposed on LAN/VPN (Tailscale recommended over port forwarding), add proper session auth and TLS. Never expose to the public internet.
- Secrets: API keys in `.env`/OS keychain, never in the DB or repo; least-privilege keys; rotate.
- GDPR: as a private individual processing your own data, GDPR's household exemption applies; it becomes relevant only if you host it for others.
- Data retention: keep raw import files (hashed) for auditability; allow full wipe of derived tables.
- Dependency hygiene: pin versions, `pip-audit`/`npm audit` in CI, no unvetted MCP servers or plugins with access to the DB.


## Feature Tiers

### MVP (Phase 1–2) — must be correct before anything else
- BUX CSV import (idempotent, schema-tolerant, warnings), manual correction UI, ISIN→ticker mapping with manual override
- Ledger: positions, average cost, realized/unrealized P/L, dividends, fees, cash, deposits/withdrawals
- Daily valuation in EUR with FX; local vs FX return decomposition
- Performance: value history, TWR, MWR/XIRR, period returns, monthly heatmap, vs MSCI World / S&P 500 / AEX / custom
- Allocation: holdings, asset type, sector, country, currency, market cap — **with ETF look-through**
- Basic risk: vol, beta, drawdown, Sharpe/Sortino, correlation matrix, risk contribution, concentration flags
- Dashboard + per-holding page with valuation/growth/quality metrics vs own history and peers
- Daily job for EOD refresh + EOD alerts (price, concentration, drawdown, earnings dates)
- Reconciliation check against BUX annual statement / Portfolio Performance

### V2 — analytics on a proven base
- Scoring (5 pillars) on a peer universe (STOXX 600 + S&P 500), score-diff explanations
- Rule-based recommendation cards with confidence and invalidators
- Monte Carlo (block bootstrap), contribution plans, what-if weight changes, historical episode replay, factor shocks
- Factor regression (FF5 + momentum), risk-parity / min-variance / HRP alternative allocations
- Backtesting harness with point-in-time discipline, permutation test, deflated Sharpe
- Fundamental and valuation alerts; news feed by ticker with sources
- AI assistant with tool-calling over the database (metrics-only mode by default)

### V3 — experimental, only if V2 earns it
- Optional intraday quotes and alerts
- GARCH volatility forecasts feeding VaR; regime indicators
- ML experiments strictly inside the backtest harness with pre-registered hypotheses
- IMAP auto-ingest of BUX export emails; mobile-friendly PWA; multi-account support
- Local LLM (Ollama) mode

### Features that should NOT be built
- ML/LSTM/GBM price prediction as a product feature (no reliable edge; high maintenance; misleading)
- "Optimal portfolio" from mean-variance with historical returns as advice
- Technical-signal trading recommendations
- Real-time streaming quotes/intraday charts (TradingView does it free; adds cost and complexity for zero long-term-investor value)
- Automated order execution of any kind
- A social/news firehose; LLM-written "market commentary"
- Scraping BUX, justETF, or Yahoo as a primary dependency
- Cloud multi-tenant hosting (privacy and GDPR burden with no benefit for a personal tool)

## Challenging the brief (senior-team review)

- **"High-end dashboard first"** is the wrong order. A beautiful dashboard on top of a cost-basis bug is worse than a table with correct numbers. Build the ledger, validate it against Portfolio Performance to the cent, then invest in UI.
- **Seven scores per stock** is over-specified. Five pillars with 3–6 evidence-backed metrics each is what quant shops do; "Fundamental score" duplicates Growth+Quality. More scores = more false precision.
- **"Prediction system is one of the most important areas."** Reframed: *uncertainty-quantification* is important; *prediction* is not achievable. A fan chart driven by editable assumptions is the honest, useful version. Any per-stock "forecast" beyond that will not survive its own backtest.
- **The recommendation engine will not generate alpha** on a personal portfolio; treat it as attention triage and never as advice. Its most valuable output is REVIEW/REDUCE (risk hygiene), not OPPORTUNITY.
- **Backtesting is only as honest as the data.** Without point-in-time EU fundamentals and delisted names, results are optimistic. Build the harness, but present results as method evidence with stated biases.
- **Alerts and news** create the most maintenance per unit of value. Keep to EOD alerts and a single news API.
- **The AI layer is cheap and useful only if constrained** to tool outputs. An unconstrained chat will confidently invent valuations. Design the constraint in from day one.
- **Fork Ghostfolio?** Tempting for the tracker. Against: TypeScript/NestJS stack would split the codebase from the Python analytics, and its data model lacks point-in-time fundamentals and look-through. Recommendation: **build our own ledger in Python (~1–2 weeks with tests), copy Ghostfolio's import/activity conventions, and validate against Portfolio Performance.**
- **Streamlit vs Next.js:** start with Streamlit for Phases 1–2 to reach trustworthy numbers fast; move to Next.js in Phase 3 once the analytics API is stable. If you *know* you will not tolerate Streamlit's look even temporarily, go Next.js from the start and accept ~30% more effort before you see your first correct portfolio number.

## Risks

**Technical**
- BUX changes the CSV format or removes the export (medium likelihood; mitigation: schema-tolerant importer, raw-file archive, generic CSV path, GDPR fallback).
- Data-vendor churn: free tiers shrink (Alpha Vantage, FMP already did), yfinance breaks (mitigation: provider interface + local cache; one paid vendor).
- ISIN→ticker mapping errors for multi-listed EU stocks (mitigation: OpenFIGI + manual override + reconciliation to BUX prices).
- Corporate actions (splits, spin-offs, ISIN changes) silently corrupting history (mitigation: corporate_action table, alerts on qty/price discontinuities).
- Single-developer maintenance load: scope discipline matters more than any framework choice.

**Data / analytical**
- Stale or missing EU fundamentals → wrong scores presented confidently (mitigation: staleness shown on every score; coverage report per holding).
- Look-ahead and survivorship bias in backtests (mitigation: §13 rules; state biases on every result).
- Overfitting via iteration (mitigation: log all variants, deflated Sharpe, permutation test).
- Estimation error in covariance/expected returns → unstable "optimal" portfolios (mitigation: shrinkage, no MVO-as-advice).
- False confidence from a polished UI: the biggest behavioural risk. Every model output must carry assumptions, data date and a plain-language uncertainty note.

**Financial**
- Acting on REDUCE/OPPORTUNITY labels increases turnover and costs without evidence of benefit. Default the tool to *inform*, with a visible "expected value of acting on this signal, from backtest" figure — usually near zero.

---

## Final Blueprint

1. **Feasibility rating: 74/100** (tracking/risk: 90; data: 60; prediction as hoped: 25; as reframed: 75).
2. **Best BUX import:** native transaction-history CSV (emailed) → upload.
3. **Alternatives:** CSV via Ghostfolio/Portfolio Performance as intermediate; manual correction UI; GDPR Art. 15/20 request. Excluded: unofficial API, scraping, OCR, PSD2.
4. **Data providers:** EODHD (prices; fundamentals at Phase 2), SEC EDGAR, Frankfurter (ECB FX), OpenFIGI, issuer ETF holdings files, Marketaux (news), Kenneth French factors, yfinance as fallback only.
5. **Stack:** Python 3.12, FastAPI, SQLAlchemy+Alembic, pandas/numpy/scipy/statsmodels/scikit-learn, PyPortfolioOpt or Riskfolio-Lib, arch (GARCH), custom or vectorbt backtester; Next.js+TypeScript+Tailwind+shadcn (Streamlit interim); Lightweight Charts + ECharts; Docker Compose; LLM via tool-calling API or Ollama.
6. **Database:** §4 schema; SQLite → PostgreSQL when needed; transactions as source of truth, derived tables rebuildable.
7. **Architecture:** local-first monolith: importers → ledger → market-data cache → analytics → API → web; scheduled EOD jobs; LLM restricted to analytics functions.
8. **MVP / 9. V2 / 10. V3:** as listed above.
11. **Prediction:** fundamental expected-return decomposition with distributions; block-bootstrap Monte Carlo; factor attribution; stress tests; GARCH for vol only; no ML price models.
12. **Scoring:** 5 pillars, sector×region percentile ranks, equal-weight, versioned, mechanical change explanation, staleness shown.
13. **Optimisation:** shrinkage covariance, risk contribution, min-variance/risk-parity/HRP alternatives with turnover constraints; MVO shown only with explicit warnings.
14. **Recommendations:** deterministic rules over scores/exposures/alerts, with confidence, invalidators, data date; LLM phrases only.
15. **Backtesting:** point-in-time with reporting lags, survivorship-free universe (EODHD constituents), walk-forward, costs, permutation null, deflated Sharpe, variant log.
16. **Security/privacy:** local Docker, encrypted disk + encrypted backups, secrets in env/keychain, LAN via Tailscale only, LLM gets normalised metrics not amounts, GDPR household exemption.
17. **Development complexity:** Phase 1 ≈ 3–4 weeks, Phase 2 ≈ 4–6 weeks, Phase 3 ≈ 6–8 weeks, Phase 4 ≈ 4–6 weeks of focused part-time work for one experienced developer; roughly 15–25k lines including tests.
18. **Recurring costs:** €20/month (start) → €55–85/month (with EU fundamentals); LLM usage < €5/month; optional Morningstar Investor ≈ €20/month equivalent if you want analyst research.
19. **Biggest technical risks:** BUX export changes; vendor churn; ISIN mapping; corporate actions.
20. **Biggest analytical risks:** stale EU fundamentals; backtest bias; overfitting; false confidence.
21. **Do not build:** ML price prediction, MVO-as-advice, technical trading signals, real-time streaming, auto-trading, scraping dependencies, cloud multi-tenant.
22. **Roadmap:** below.

## Development Roadmap

| Phase | Objective | Features | Dependencies | Complexity | Expected result |
|---|---|---|---|---|---|
| **1 — Trustworthy ledger** | Correct portfolio numbers from BUX data | CSV importer + tests on your real export; ledger; FX; EOD prices; positions, P/L, TWR/MWR; reconciliation vs BUX statement/Portfolio Performance; minimal UI (Streamlit) | One BUX export from you; EODHD EOD key; Frankfurter; OpenFIGI | Medium | You open the app and every euro matches BUX |
| **2 — Insight** | Understand allocation, performance and risk | Look-through allocation; benchmarks; return decomposition (stock vs FX); risk stats; correlation & risk contribution; concentration flags; EOD alerts; holdings pages with fundamentals (EDGAR for US, EODHD for EU) | EODHD Fundamentals (or start US-only); issuer ETF holdings downloader | Medium–High | Answers "how am I doing, versus what, and where is my risk" |
| **3 — Judgement** | Explainable scores, scenarios, evidence | Peer universe; 5-pillar scores + diffs; recommendation cards; Monte Carlo + what-if + stress; factor regression; alternative allocations; **backtesting harness and first honest results**; Next.js dashboard | Universe fundamentals; index constituent history; French factor data | High | Scores you can defend, scenarios you can compare, and a backtest that tells you whether the scores mean anything |
| **4 — Assistant & polish** | Ask questions, get cited answers | Tool-calling LLM over analytics; news by ticker; fundamental/valuation alerts; IMAP auto-import; local-LLM option; encrypted backups; PWA | Phase 3 API stable | Medium | One dashboard + a chat that only says what the data supports |

## What I need from you to start Phase 1
1. **One BUX transaction-history CSV export** (anonymise amounts if you like; keep headers, a buy, a sell, a dividend, a fee, a deposit, and any corporate-action row). Why: the importer must be built and tested against the 2026 format, not the converter's inferred one. Impact: removes the single largest unknown.
2. **Confirmation of the export menu path and daily limit** in the current app (1 minute).
3. **Rough portfolio shape**: number of positions, share of ETFs vs single stocks, exchanges (e.g. "20 positions, 4 UCITS ETFs, mostly US + Euronext"). Why: decides whether Phase 2 can start US-only on free fundamentals or needs the EODHD Fundamentals tier immediately.
4. **Decisions**: (a) Streamlit-first or Next.js-first; (b) willingness to pay ≈ €20/month now and ≈ €60/month from Phase 2; (c) cloud LLM with normalised metrics vs local model.

No installation, subscription, or connection will be made without your explicit approval.
