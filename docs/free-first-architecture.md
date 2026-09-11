# Free-First Architecture — €0/month Revision

*Revision of [feasibility-report.md](feasibility-report.md) under the hard constraint of €0 recurring cost. Date: 11 September 2026. The original research is reused; only the data strategy, feature tiers, phases and score change. New research this round: ESEF/XBRL fundamentals for EU companies, issuer ETF holdings files, current state of free price sources, local LLM hardware. Claims marked **(unverified)** could not be confirmed from this environment; two are trivially checkable on your PC (see §H).*

---

## E. €0 Feasibility Score: 66 / 100 (was 74)

| Area | Paid (74 total) | €0 | Why it changed |
|---|---|---|---|
| BUX import, ledger, P/L, TWR/MWR, FX decomposition | 90 | **90** | Unchanged. Needs only the CSV + ECB rates. |
| Prices, benchmarks, technicals, charts | 85 | **75** | yfinance is unofficial and needs periodic maintenance; Stooq now gated behind a key. Quality of EOD data is fine; *reliability of access* is the cost. |
| Risk analytics, Monte Carlo, stress, correlation, factor regression | 90 | **88** | Unchanged in substance; French/AQR factor data is free. |
| US fundamentals | 80 | **80** | SEC EDGAR is free, authoritative and point-in-time. Better than most paid feeds. |
| EU fundamentals | 65 | **45** | ESEF filings on filings.xbrl.org are free but **annual-only**, from FY2020, with per-company tag mapping work. No quarterly data, no estimates. |
| ETF look-through | 60 | **55** | iShares is scriptable daily; Vanguard monthly; Xtrackers/Amundi/SPDR/Invesco vary and need per-issuer adapters. |
| Peer-universe scoring | 65 | **45** | Free universe fundamentals must come from ESEF (EU, annual) + EDGAR (US) + Yahoo ratios; heterogeneous quality, real maintenance. |
| Backtesting of scores | 60 | **35** | No free survivorship-free historical index constituents; EU point-in-time only from FY2020. Backtests become "indicative on current constituents" only. |
| Analyst estimates, earnings calendar | 60 | **25** | Yahoo only, thin for EU. Treated as "unavailable in free mode" for most EU names. |
| Local AI assistant | 80 | **75** | Fully feasible on a normal PC with a 7–14B model; slower and less eloquent than cloud, adequate for templated explanation. |
| News | 70 | **65** | Company IR RSS, GDELT, Marketaux free tier. Fine for a watchlist. |
| Maintenance burden | — | −5 | Free sources change silently; provider abstraction + cache + fallbacks mitigate but do not remove this. |

**Summary of the delta:** everything that depends on *your* data plus prices stays essentially intact (this is 70% of the daily value). What degrades is *breadth and history of third-party fundamentals*: EU coverage becomes annual and partially hand-mapped, estimates mostly vanish, and honest backtesting of the scoring layer becomes much weaker. Nothing becomes impossible, but the scoring/recommendation/backtest tier must be labelled as "indicative" rather than "validated".

---

## A. €0 Architecture

Everything runs on your PC. Outbound traffic is only to public data sources. No account, key or subscription is required for the core; a few free keys (Alpha Vantage, Twelve Data, Marketaux) are optional supplements.

```
┌──────────────────────────── Your PC (Docker Compose or plain venv) ────────────────────────────┐
│                                                                                                │
│  Web UI  (Streamlit → later Next.js)                                                           │
│      │ HTTP                                                                                    │
│  FastAPI backend                                                                               │
│      ├── importers/        bux_csv (idempotent, schema-tolerant), generic_csv, manual edits    │
│      ├── core/             ledger, cost basis, FX, valuation, TWR/MWR, decomposition           │
│      ├── marketdata/       MarketDataProvider interface + router + SQLite cache               │
│      │     ├── prices:      YahooProvider → StooqProvider → TwelveDataProvider → AlphaVantage  │
│      │     ├── fx:          ECBProvider (Frankfurter mirror or ECB Data Portal SDMX)           │
│      │     ├── identifiers: OpenFIGIProvider, YahooSearch (ISIN→ticker), manual override      │
│      │     ├── fundamentals: SECEdgarProvider (US, XBRL companyfacts)                          │
│      │     │                 ESEFProvider (EU, filings.xbrl.org + Arelle/pyesef parsing)       │
│      │     │                 YahooInfoProvider (ratios/estimates, best-effort, flagged)        │
│      │     ├── etf_holdings: IsharesAdapter, VanguardAdapter, XtrackersAdapter, ...            │
│      │     ├── factors:     KennethFrenchProvider, AQRProvider                                 │
│      │     ├── news:        CompanyIRRSSProvider, GDELTProvider, MarketauxProvider(free key)   │
│      │     └── [optional, disabled by default] EODHDProvider, CloudLLMProvider                 │
│      ├── analytics/        risk, factors, scores, recommend, montecarlo, whatif, backtest     │
│      ├── alerts/           EOD rule evaluation, in-app inbox, optional ntfy/email             │
│      ├── ai/               tool registry → Ollama (local) ; cloud adapter optional            │
│      └── jobs/             APScheduler: EOD refresh, weekly fundamentals, monthly ETF holdings │
│                                                                                                │
│  SQLite (personal + cache)      Raw file archive (BUX CSVs, filings, holdings files)           │
│  Ollama (optional local LLM)                                                                   │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Provider abstraction (the load-bearing design decision)

```python
class MarketDataProvider(Protocol):
    name: str; cost: Literal["free","free-key","paid"]; capabilities: set[str]
    def get_quote(self, ids: SecurityIds) -> Quote | None
    def get_eod_history(self, ids, start, end) -> DataFrame | None
    def get_dividends(self, ids) -> DataFrame | None
    def get_corporate_actions(self, ids) -> DataFrame | None
    def get_fundamentals(self, ids) -> FundamentalsBundle | None   # statements + as_reported_at + source per field
    def get_estimates(self, ids) -> Estimates | None
    def get_fx_rate(self, base, quote, date) -> Decimal | None
    def get_etf_holdings(self, ids) -> Holdings | None
    def get_news(self, ids, since) -> list[NewsItem]
    def resolve_identifiers(self, isin) -> SecurityIds | None
```

Rules:
1. **Router, not provider, is called by the app.** The router holds an ordered list per capability, tries providers in order, records `source`, `fetched_at`, `quality_flag` on every stored value, and never raises on a miss: it returns `None` and the UI renders "unavailable (no free source)".
2. **Cache-first.** Every fetched series/statement is stored; jobs refresh incrementally. A dead provider means stale data, not a broken app. Staleness is displayed.
3. **Merge with provenance.** Fundamentals may be assembled field-by-field (revenue from ESEF, EPS from Yahoo, FCF computed). Every field carries its own source; derived fields carry their formula.
4. **Provider health page.** Success rate and last error per provider, so you see a breakage before the analytics silently degrade.
5. **Paid providers are just more classes**, enabled by adding a key to `.env`. Nothing else changes.

### Graceful degradation contract
Each analytic declares its required inputs. The scoring engine computes a pillar only when ≥ 3 of its metrics exist and reports **coverage** ("Quality: 4/6 metrics, 2 unavailable in free mode"). Overall score is a coverage-weighted mean and is suppressed below 60% coverage. Recommendation rules that reference missing inputs are skipped and the card says which rules could not be evaluated. No metric is ever imputed silently.

---

## B. Free Data-Source Map

| Data | Primary free source | Fallback | Quality | Limitations / notes |
|---|---|---|---|---|
| **Transactions, cost basis, dividends, fees, broker FX** | BUX CSV export (§1 of original report) | Manual entry/correction UI; GDPR request | Excellent | Manual trigger; 3 exports/day; schema may change |
| **FX rates (EUR base)** | ECB reference rates via Frankfurter (frankfurter.dev) or ECB Data Portal SDMX API | Cached history; Yahoo `EURUSD=X` | Excellent | Business days only, daily fix; fine for valuation |
| **ISIN → ticker mapping** | OpenFIGI (free key optional) + Yahoo search/lookup | Manual mapping table (you confirm once per holding) | Good | Multi-listings need the exchange choice; store the mapping |
| **EOD prices, splits, dividends (US + EU + UCITS ETFs)** | **yfinance** (Yahoo) with `curl_cffi` session, ~1 request/2 s, cached | Stooq (free key via CAPTCHA/email since Apr 2026), Twelve Data free (800 credits/day), Alpha Vantage free (25/day), `bf4py` for Xetra **(grey ToS)** | Good | Unofficial; breaks a few times a year, fixed upstream within days. For ~30 holdings + 5 benchmarks + 1,000-name universe, one full daily refresh ≈ 1,000 requests — spread over the day, well inside Yahoo's soft limits. Yahoo ToS: personal, non-commercial, no redistribution. Acceptable for a private local tool; design so a breakage costs you nothing but freshness. |
| **Intraday / "today's change"** | Yahoo delayed quote via yfinance | none | Adequate | 15-min delayed; do not build intraday features |
| **Benchmarks** (MSCI World, S&P 500, AEX, Nasdaq-100) | Yahoo: `IWDA.AS`, `^GSPC`/`VUSA.AS`, `^AEX`, `^NDX`; use EUR-listed UCITS ETFs so the benchmark is already in EUR and includes dividends | Stooq | Good | Use accumulating ETFs as total-return proxies |
| **US fundamentals (statements, EPS, shares, with filing dates)** | **SEC EDGAR** `companyfacts` XBRL API + Financial Statement Data Sets | none needed | Excellent, point-in-time | 10 req/s, User-Agent required; quarterly + annual; US filers only (incl. foreign 20-F filers such as ASML, which file XBRL too — check per holding) |
| **EU fundamentals (annual statements)** | **filings.xbrl.org** ESEF index (JSON:API) → download report package → parse with **Arelle / pyesef** → map IFRS tags to ~15 metrics | Yahoo `Ticker.info` / `financials` (best-effort, unaudited by us), company IR PDFs (manual) | **Variable** | Annual only, from FY2020; ~3,000+ filings incl. NL (AFM), DE, FR, BE, UK **(coverage per country unverified)**; IFRS core tags are consistent for revenue, net income, equity, assets, cash; **operating profit, EPS variants, capex and debt often use company extensions** → per-company mapping with fallbacks; publication date = filing date (good for point-in-time). Site reserves the right to rate-limit. ESAP (EU official portal) only starts collecting in July 2026 and is not usable before 2027. |
| **EU quarterly / half-year fundamentals** | none structured | Yahoo `quarterly_financials` (thin for EU), IR PDFs | Poor | **Mark "unavailable in free mode"** for most EU names. Do not build a PDF-extraction pipeline for this (§F). |
| **Analyst estimates, target prices, earnings dates** | Yahoo via yfinance (`earnings_dates`, `analyst_price_targets`, `eps_trend`) | none | Poor for EU, fair for US | Best-effort, flagged; estimate-revision metrics become "unavailable" where n_analysts < 3 |
| **Sector / industry / country / market cap** | Yahoo `info` (GICS-like sector/industry), ESEF entity data, ETF holdings files (sector/country per constituent) | Manual tag per holding (30 rows, once) | Good | Yahoo sector taxonomy is coarse but consistent |
| **UCITS ETF holdings (look-through)** | **Issuer files**: iShares (daily CSV, stable `.ajax?fileType=csv&dataType=fund` URL per product — has ISIN, ticker, sector, country, currency, weight); Vanguard (monthly CSV from fund page); SPDR/Invesco (XLSX/CSV on product page **(unverified)**); Xtrackers, Amundi (PDF/XLS, less machine-friendly **(unverified)**) | Yahoo top-10 holdings + sector weights (thin); manual entry from factsheet | Variable by issuer | One adapter per issuer; iShares alone covers a large share of typical NL portfolios (IWDA, EIMI, CSPX…). Terms: personal download of published files is normal use; keep frequency low (monthly is enough) and cache. |
| **Factor returns** (Mkt, SMB, HML, RMW, CMA, Mom; US / Europe / Global) | Kenneth French Data Library (monthly & daily CSV zips) | AQR datasets | Excellent | Monthly lag of a few weeks; fine |
| **Risk-free rate** | ECB Data Portal (€STR / deposit rate) or FRED (DFF) | constant | Excellent | |
| **Macro** | FRED, ECB Data Portal, Eurostat | — | Excellent | Context only |
| **News** | Company IR RSS feeds (per holding, once), GDELT DOC API (free, no key), Marketaux free key (100/day) | NewsAPI free (24 h delay, non-commercial) | Good | Tagging by ticker is the weak spot; IR feeds are the highest-signal, lowest-noise source |
| **Filings alerts** | SEC EDGAR RSS (US), filings.xbrl.org sort by `-added_time` (EU annual), IR RSS | — | Good | |
| **Historical index constituents** (survivorship-free backtests) | none free | Current constituents from issuer ETF holdings files (snapshot) | **Unavailable** | Backtests limited to current-constituent universes with the bias stated |
| **LLM** | Ollama with an open-weight 7–14B instruct model | llama.cpp; cloud adapter optional | Adequate | See §I hardware |

---

## Free-Fundamentals Strategy (detail)

**US (and US-filing foreign issuers): solved.** EDGAR `companyfacts` returns every reported XBRL fact with fiscal period and filing date. Build `FundamentalsBundle` from a mapping of ~20 us-gaap/ifrs-full concepts; compute TTM, FCF (= OCF − capex), margins, ROIC, net debt, growth. This is the same data paid vendors resell.

**EU: layered.**
1. **ESEF via filings.xbrl.org** — the one genuinely free structured source. Pipeline: query API by LEI/entity → download xBRL-JSON or report package → extract facts under the IFRS taxonomy → map to the metric table with a fallback list per metric (e.g. operating profit: `ifrs-full:ProfitLossFromOperatingActivities` → company extension containing "OperatingProfit" → EBIT computed as pre-tax profit + finance costs) → store with `as_reported_at` = filing date and `mapping_confidence`. Expect 70–85% of the 15 metrics to map automatically for large caps and a UI "fix mapping" step for the rest, done once per company and reused every year.
2. **Yahoo best-effort** for ratios/quarterlies/estimates, always flagged `quality=unverified`.
3. **Manual entry** from the company's own annual report for a holding that fails both (a 5-minute form per company per year; acceptable for ≤ 30 holdings).
4. **Not built**: a generic PDF-statement extraction pipeline. Reason: ESEF already delivers the same annual figures as structured data; PDF extraction would only add quarterlies, at very high maintenance and error rates. Quarterlies for EU names are marked **unavailable in free mode**. If it ever matters, that is the one thing a paid feed buys.

**Peer universe for scoring (needed for percentile ranks):** US — EDGAR bulk "Financial Statement Data Sets" cover every filer for free; EU — bulk ESEF for STOXX 600 constituents is a one-time ~600-filing job per year, mapped with the same rules; expect lower mapping confidence on the tail. Universe prices from Yahoo, refreshed weekly (not daily). This is feasible but it is the single largest engineering chunk in the free plan; it is scheduled in Phase 4 and the scoring layer works in "own-history + sector medians from Yahoo" mode before that.

---

## Free vs Paid Feature Matrix

| Feature | Free | Paid needed | Quality in free mode |
|---|---|---|---|
| BUX import, ledger, corrections | ✅ | ❌ | Excellent |
| Positions, P/L, dividends, fees, cash flows | ✅ | ❌ | Excellent |
| TWR / MWR / XIRR, period returns | ✅ | ❌ | Excellent |
| FX decomposition (stock vs currency) | ✅ | ❌ | Excellent |
| EOD prices US + EU + UCITS ETFs | ✅ | ❌ | Good (unofficial access; cached) |
| Today's change (delayed) | ✅ | ❌ | Adequate |
| Benchmarks | ✅ | ❌ | Good |
| Charts, technical indicators | ✅ | ❌ | Good |
| Allocation by sector/country/currency/market cap | ✅ | ❌ | Good (Yahoo taxonomy) |
| ETF look-through | ⚠️ | ❌ | Good for iShares/Vanguard; variable for others; manual fallback |
| Risk: vol, beta, drawdown, Sharpe, Sortino, correlation, risk contribution, concentration | ✅ | ❌ | Excellent |
| VaR / CVaR, GARCH vol | ✅ | ❌ | Excellent |
| Factor regression (FF5 + Mom) | ✅ | ❌ | Excellent |
| Monte Carlo, stress tests, what-if, contribution plans | ✅ | ❌ | Excellent |
| Alternative allocations (min-var, risk parity, HRP) | ✅ | ❌ | Excellent |
| US fundamentals (quarterly, point-in-time) | ✅ | ❌ | Excellent |
| EU fundamentals — annual | ⚠️ | ❌ | Variable (ESEF mapping) |
| EU fundamentals — quarterly | ❌ | Optional (EODHD) | Unavailable in free mode |
| Analyst estimates / earnings dates | ⚠️ | Optional | Fair US, poor EU |
| Valuation vs own history | ✅ | ❌ | Good (price history × annual/TTM fundamentals) |
| Valuation vs sector peers | ⚠️ | Optional | Yahoo sector medians (rough) until Phase 4 universe |
| Stock scoring (5 pillars) | ⚠️ | Optional improves | Indicative; coverage shown per pillar |
| Explainable recommendations | ✅ | ❌ | Good for risk hygiene (REVIEW/REDUCE); weaker for OPPORTUNITY |
| Score change explanations | ✅ | ❌ | Good |
| Backtesting of own allocation / simple rules on current holdings | ✅ | ❌ | Good, biases stated |
| Backtesting of scoring on survivorship-free universe | ❌ | Optional (EODHD constituents) | Unavailable in free mode |
| News by holding | ✅ | ❌ | Good (IR RSS + GDELT) |
| EOD alerts (price, valuation, concentration, drawdown, filings, technical) | ✅ | ❌ | Good |
| Intraday alerts | ❌ | Optional | Not planned |
| Local AI assistant over database functions | ✅ | ❌ | Adequate–good, hardware-dependent |
| Cloud LLM quality | ❌ | Optional | Better prose, same numbers |

---

## D. Paid Upgrade Map (optional, never required)

| If you ever pay for… | What improves | Cost (Sept 2026) |
|---|---|---|
| EODHD All-World EOD | Official, stable price access; removes yfinance maintenance; historical index constituents (survivorship-free backtests) | $19.99/mo |
| EODHD Fundamentals | EU quarterly statements, estimates, ETF holdings for many UCITS funds, consistent field names across 1,000+ names → scoring quality and coverage jump; Phase 4 ESEF mapping effort becomes optional | $59.99/mo (includes EOD) |
| Twelve Data Grow | Official prices, some fundamentals | ~$29/mo |
| Cloud LLM (Anthropic/OpenAI) | Faster, more fluent explanations; same underlying numbers | ~€2–5/mo at personal usage |
| Morningstar Investor | Human analyst research, X-Ray; used *beside* the tool, not in it | ~€20/mo equivalent |

The router makes each of these a config change. Nothing in the free design is thrown away.

---

## F. Biggest Compromises (honest list)

1. **Price access is unofficial.** yfinance works for personal use and is widely used, but Yahoo can and does change things; expect to bump the library a few times a year. A breakage costs freshness, not data, thanks to the cache. Stooq's new key requirement makes it a weaker fallback than it was.
2. **EU fundamentals are annual and partially hand-mapped.** A holding that reports in June will show 9–12-month-old fundamentals by the next spring. Trend metrics on annual data have 3–5 data points. Quality/growth pillars for EU names are coarser than for US names, and the UI must say so.
3. **No credible estimate data for EU names.** Forward P/E, PEG, estimate revisions, "guidance change" alerts: unavailable or thin for most EU holdings.
4. **Peer-relative scoring arrives late (Phase 4) and stays rougher.** Until the free universe exists, scores compare to a stock's own history and coarse sector medians.
5. **Scoring cannot be honestly backtested on a survivorship-free universe.** Backtests are limited to current constituents; results carry an explicit upward bias note. In practice this means the recommendation engine remains a *monitoring* tool, which the original report already argued it should be.
6. **ETF look-through is uneven** across issuers; some funds may need a manual factsheet entry each quarter.
7. **Maintenance is the recurring cost** instead of money: a realistic estimate is 1–3 hours per month of upkeep once built.
8. **Local AI is slower and less articulate** than cloud models. For "explain these numbers" it is sufficient.

Nothing is lost that affects the core questions: how am I doing, versus what, where is my risk, what would happen if.

---

## C. Revised MVP and Phases

The ordering you proposed is right with two changes: (1) prices move into Phase 1 because a portfolio value without prices is not a dashboard, and (2) US fundamentals (EDGAR) move earlier than EU fundamentals because they are cheap and reliable and give the stock page real content early.

| Phase | Objective | Build | Data sources introduced | Complexity | Result |
|---|---|---|---|---|---|
| **1 — BUX Portfolio Core** | Every euro matches BUX | Provider abstraction + cache + health page; BUX CSV importer with tests on your real file; ISIN mapping (OpenFIGI/Yahoo + manual); ledger, cost basis, dividends, fees, deposits/withdrawals, cash; ECB FX; daily valuation history; TWR/MWR; reconciliation report; minimal Streamlit dashboard (overview, holdings table, value chart) | BUX CSV, ECB, OpenFIGI, Yahoo prices | Medium | Trustworthy numbers, first dashboard |
| **2 — Free Market Intelligence** | Understand performance and exposure | Benchmarks (EUR UCITS proxies); return decomposition stock vs FX; period returns and heatmap; technical indicators; sector/country/currency/market-cap allocation via Yahoo metadata; iShares + Vanguard look-through adapters; risk metrics, correlation, risk contribution, drawdown, beta; watchlist; EOD price/concentration/drawdown alerts | Yahoo metadata, issuer holdings files (iShares, Vanguard) | Medium | Answers "how am I doing, vs what, where is the risk" |
| **3 — Portfolio Intelligence** | Explainable judgement and scenarios | US fundamentals from EDGAR → valuation/growth/quality metrics vs own history; Monte Carlo (block bootstrap), stress tests, what-if, contribution plans; factor regression (French data); alternative allocations (min-var, risk parity, HRP); scoring v1 (own-history + sector-median mode, coverage-aware); rule-based recommendations with confidence and invalidators; valuation/technical alerts; news via IR RSS + GDELT; simple backtests of allocation rules on current holdings | EDGAR, French/AQR, IR RSS, GDELT | High | Scores, scenarios and recommendations you can explain and audit |
| **4 — Advanced Intelligence** | Broaden fundamentals, add the assistant | ESEF pipeline (filings.xbrl.org + Arelle/pyesef) with mapping UI; remaining ETF issuer adapters; peer universe (EDGAR bulk + ESEF STOXX 600) → percentile scoring v2 and score diffs; Yahoo best-effort estimates flagged; backtest harness with stated biases and permutation test; local LLM assistant over the tool registry (Ollama); filings alerts; Next.js dashboard; optional IMAP auto-import | filings.xbrl.org, EDGAR bulk, Ollama | High | Fuller stock research, universe-relative scores, cited AI explanations |

**Exactly what we build first (Phase 1, in order):** provider interface + SQLite cache → BUX importer (against your file) → ledger and cost-basis tests → FX → Yahoo price provider with cache and throttling → valuation history and TWR/MWR → reconciliation against your BUX annual statement → Streamlit page. Roughly 3–4 weeks part-time. No AI, no scores, no news in Phase 1.

---

## I. Hardware for Local AI

The assistant's job is narrow: call functions, then write a few paragraphs that restate the returned numbers with the required Facts / Calculations / Interpretation / Uncertainty structure. This is well within reach of current open-weight 7–14B instruct models with tool-calling support (Qwen, Llama, Gemma, Mistral families at 4-bit quantisation), served by **Ollama** (simplest) or llama.cpp.

| Tier | Hardware | Model class | Experience |
|---|---|---|---|
| **Minimum** | Any 64-bit PC with **16 GB RAM**, no GPU | 7–8B at Q4 (≈ 5 GB) | 5–12 tokens/s on CPU; a 300-word answer in 30–60 s. Usable. |
| **Recommended** | 16–32 GB RAM plus a GPU with **8–12 GB VRAM** (e.g. RTX 3060 12 GB / 4070), or an Apple Silicon Mac with 16–24 GB unified memory | 8–14B at Q4–Q5 | 20–50 tokens/s; answers in a few seconds; comfortable |
| **High-end** | 24 GB+ VRAM (RTX 3090/4090) or Mac with 32–64 GB | 27–32B dense, or 30B-class MoE models | Noticeably better reasoning and prose; not required for this workload |

**Do not buy hardware for this.** If your current PC has 16 GB RAM, start with a 7–8B model; if it has any recent discrete GPU or is an Apple Silicon Mac, you are already in the recommended tier. The deterministic engine does all arithmetic, so a small model's weaknesses (maths, long-context recall) are designed out. Tell me your PC's RAM/GPU and I will name a specific model at build time rather than guessing now.

---

## G. Recommendation

**Yes, build it under the €0 constraint**, with the phase order above. The free plan preserves everything that answers your core questions and costs you maintenance time instead of money. The compromises fall exactly on the features the original report already rated as lowest-value-per-effort (peer scoring breadth, estimate-driven alerts, validated backtests of a recommendation engine). Two conditions:

1. Accept that Phase 4's EU-fundamentals and universe work is the one place where "free" is genuinely expensive in effort; treat it as optional depth, not as a gate for the rest.
2. Keep the provider abstraction and the degradation contract non-negotiable from day one. They are what make "€0 now, maybe paid later" a config change instead of a rewrite.

---

## H. Two 10-second checks to run on your PC (this sandbox's proxy blocks both hosts)

```bash
# 1. filings.xbrl.org API returns Dutch ESEF filings with report-package links
curl -s 'https://filings.xbrl.org/api/filings?filter%5Bcountry%5D=NL&page%5Bsize%5D=2&include=entity' | head -c 1500

# 2. iShares publishes full holdings as CSV without a key (IWDA example)
curl -sL -A 'Mozilla/5.0' 'https://www.ishares.com/nl/particuliere-belegger/nl/producten/251882/ishares-msci-world-ucits-etf-acc-fund/1506575576011.ajax?fileType=csv&fileName=IWDA_holdings&dataType=fund' | head -20
```
If both print sensible content, the two most important free-mode assumptions are confirmed.

## What I still need from you before Phase 1
1. One BUX transaction-history CSV (anonymised amounts are fine; keep headers and one row of each type).
2. Output of the two checks above (or just "both worked").
3. Your PC's RAM and GPU (for the local model choice, Phase 4).
4. Portfolio shape: number of holdings, which ETFs (issuers), share of US vs EU names.
5. Approval of this plan, and the Streamlit-first choice for Phases 1–3.

No packages, keys, services or models will be installed or connected without your explicit approval.
