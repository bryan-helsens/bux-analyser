"""Yahoo Finance via yfinance. Free, unofficial; personal non-commercial use.

Design for breakage: every call is wrapped, throttled, and returns None on failure so the
router/cache can degrade gracefully. Never raises into the application.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd

from .base import PriceSeries, Provenance, ProviderHealth, Quote, SecurityIds, SecurityMeta
from .fundamentals import FY, Q, UNVERIFIED, FinancialPeriod, Fundamentals

log = logging.getLogger(__name__)

# Prefer EUR/home listings for a EUR investor; order matters when several listings match.
_EXCHANGE_PREFERENCE = ["AMS", "GER", "PAR", "BRU", "LSE", "EBS", "MIL", "NMS", "NYQ", "NGM", "PCX", "ASE"]


class YahooProvider:
    name = "yahoo"
    cost = "free"
    source = "Yahoo Finance (yfinance, unofficial)"

    def __init__(self, min_interval_s: float = 1.5):
        self.min_interval_s = min_interval_s
        self._last_call = 0.0
        self.health = ProviderHealth(self.name)

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # ---- identifier resolution -------------------------------------------------
    @staticmethod
    def rank_candidates(cands: list[dict], hint_currency: str | None) -> list[dict]:
        """Order listings: trading-currency match first (BUX trades many US names in EUR),
        then preferred exchanges, then equities/ETFs before other quote types."""
        hc = (hint_currency or "").upper()

        def key(c):
            ccy = (c.get("currency") or "").upper()
            ex = c.get("exchange") or ""
            ccy_rank = 0 if (hc and ccy == hc) else (1 if not ccy else 2)
            ex_rank = _EXCHANGE_PREFERENCE.index(ex) if ex in _EXCHANGE_PREFERENCE else 99
            type_rank = 0 if c.get("quoteType") in ("EQUITY", "ETF", None) else 1
            return (ccy_rank, ex_rank, type_rank)
        return sorted(cands, key=key)

    def _search(self, query: str) -> list[dict]:
        import yfinance as yf
        self._throttle()
        try:
            return [q for q in (yf.Search(query, max_results=10).quotes or []) if q.get("symbol")]
        except Exception as e:
            log.info("yahoo search failed for %r: %s", query, e)
            return []

    def resolve(self, isin: str, hint_name: str | None = None, hint_currency: str | None = None) -> SecurityIds | None:
        import yfinance as yf
        try:
            seen: dict[str, dict] = {}
            for q in self._search(isin):
                seen.setdefault(q["symbol"], q)
            if hint_name and (hint_currency and not any((q.get("currency") or "").upper() == hint_currency.upper() for q in seen.values())):
                for q in self._search(hint_name):
                    seen.setdefault(q["symbol"], q)
            if not seen:
                self._throttle()
                t = yf.Ticker(isin)  # Yahoo often accepts an ISIN directly
                info = t.info or {}
                if info.get("symbol"):
                    seen[info["symbol"]] = {"symbol": info["symbol"], "exchange": info.get("exchange"),
                                            "shortname": info.get("shortName"), "currency": info.get("currency")}
            if not seen:
                self.health.fail(f"no symbol for {isin}")
                return None
            # fill in quote currency where the search result lacks it (a few cheap calls)
            for q in list(seen.values())[:6]:
                if not q.get("currency"):
                    self._throttle()
                    try:
                        q["currency"] = yf.Ticker(q["symbol"]).fast_info.currency
                    except Exception:
                        pass
            best = self.rank_candidates(list(seen.values()), hint_currency)[0]
            ccy = (best.get("currency") or hint_currency or "").upper() or None
            self.health.ok()
            return SecurityIds(isin=isin, ticker=best["symbol"], currency=ccy,
                               name=best.get("shortname") or best.get("longname") or hint_name,
                               exchange=best.get("exchange"))
        except Exception as e:
            self.health.fail(e)
            return None

    # ---- prices ----------------------------------------------------------------
    def eod_history(self, ids: SecurityIds, start: date, end: date) -> PriceSeries | None:
        import yfinance as yf
        if not ids.ticker:
            return None
        self._throttle()
        try:
            df = yf.Ticker(ids.ticker).history(start=start, end=end + timedelta(days=1),
                                               auto_adjust=False, actions=False, raise_errors=True)
            if df is None or df.empty:
                self.health.fail(f"empty history {ids.ticker}")
                return None
            s = df["Close"].astype(float)
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            s = s[~s.index.duplicated(keep="last")].dropna()
            ccy = ids.currency or (yf.Ticker(ids.ticker).fast_info.currency or "").upper()
            if ccy == "GBP" and s.median() > 1000:  # LSE quotes in pence
                s = s / 100.0
            self.health.ok()
            return PriceSeries(series=s, currency=ccy,
                               provenance=Provenance(self.name, self.source, Provenance.now(),
                                                     s.index[-1].date(), "reported"))
        except Exception as e:
            self.health.fail(e)
            return None

    # Yahoo's statement row labels, in the order we prefer them.
    _STATEMENT_ROWS = {
        "revenue": ("Total Revenue", "Operating Revenue"),
        "gross_profit": ("Gross Profit",),
        "operating_income": ("Operating Income", "EBIT"),
        "net_income": ("Net Income", "Net Income Common Stockholders"),
        "eps_diluted": ("Diluted EPS",),
        "operating_cash_flow": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
        "capex": ("Capital Expenditure",),
        "cash": ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"),
        "total_debt": ("Total Debt",),
        "total_equity": ("Stockholders Equity", "Total Equity Gross Minority Interest"),
        "total_assets": ("Total Assets",),
        "shares_diluted": ("Diluted Average Shares",),
    }

    _QUOTE_TYPE_TO_ASSET = {"EQUITY": "stock", "ETF": "etf", "MUTUALFUND": "fund",
                            "INDEX": "benchmark", "CRYPTOCURRENCY": "crypto"}

    def metadata(self, ids: SecurityIds) -> SecurityMeta | None:
        """Sector, industry, country and size. Yahoo leaves most of these empty for
        ETFs and for many European listings, which the caller must tolerate."""
        import yfinance as yf
        if not ids.ticker:
            return None
        self._throttle()
        try:
            info = yf.Ticker(ids.ticker).info or {}
            if not info:
                self.health.fail(f"no info for {ids.ticker}")
                return None
            self.health.ok()
            cap = info.get("marketCap") or info.get("totalAssets")
            return SecurityMeta(
                asset_type=self._QUOTE_TYPE_TO_ASSET.get((info.get("quoteType") or "").upper()),
                sector=info.get("sector") or info.get("category"),
                industry=info.get("industry"),
                country=info.get("country"),
                market_cap=float(cap) if cap else None,
                long_name=info.get("longName") or info.get("shortName"),
                provenance=Provenance(self.name, self.source, Provenance.now(), date.today(),
                                      "reported", "company profile"))
        except Exception as e:
            self.health.fail(e)
            return None

    def fundamentals(self, ids: SecurityIds) -> Fundamentals | None:
        """Financial statements as Yahoo reports them.

        Marked unverified: Yahoo publishes no filing date and no audit trail, so these
        figures must never be used for a point-in-time backtest. They exist so European
        holdings, which have no free authoritative feed, show something rather than
        nothing.
        """
        import yfinance as yf
        if not ids.ticker:
            return None
        self._throttle()
        try:
            t = yf.Ticker(ids.ticker)
            periods: list[FinancialPeriod] = []
            currency = None
            try:
                currency = (t.fast_info.currency or "").upper() or None
            except Exception:
                pass
            for period_type, statements in ((FY, ("income_stmt", "balance_sheet", "cashflow")),
                                            (Q, ("quarterly_income_stmt", "quarterly_balance_sheet",
                                                 "quarterly_cashflow"))):
                frames = []
                for attr in statements:
                    try:
                        frames.append(getattr(t, attr))
                    except Exception:
                        frames.append(None)
                periods += parse_yahoo_statements(frames, period_type, currency, self.source)
            if not periods:
                self.health.fail(f"no statements for {ids.ticker}")
                return None
            self.health.ok()
            return Fundamentals(
                isin=ids.isin, ticker=ids.ticker, currency=currency,
                periods=sorted(periods, key=lambda p: (p.period_end, p.period_type)),
                provenance=Provenance(self.name, self.source, Provenance.now(), date.today(),
                                      UNVERIFIED, "no filing dates published"),
                notes=["Yahoo does not publish filing dates, so these figures cannot be used "
                       "for point-in-time analysis and may be stale or restated."])
        except Exception as e:
            self.health.fail(e)
            return None

    def quote(self, ids: SecurityIds) -> Quote | None:
        import yfinance as yf
        if not ids.ticker:
            return None
        self._throttle()
        try:
            fi = yf.Ticker(ids.ticker).fast_info
            price = fi.last_price
            if price is None:
                self.health.fail(f"no last price {ids.ticker}")
                return None
            ccy = (fi.currency or ids.currency or "").upper()
            prev = fi.previous_close
            if ccy == "GBP" and price > 1000:
                price, prev = price / 100.0, (prev / 100.0 if prev else prev)
            self.health.ok()
            return Quote(price=float(price), currency=ccy, previous_close=float(prev) if prev else None,
                         provenance=Provenance(self.name, self.source, Provenance.now(), date.today(),
                                               "reported", "delayed quote"))
        except Exception as e:
            self.health.fail(e)
            return None


def parse_yahoo_statements(frames, period_type: str, currency: str | None,
                           source: str) -> list[FinancialPeriod]:
    """Fold income, balance-sheet and cash-flow frames into periods.

    Yahoo returns each statement as a DataFrame with metrics down the rows and period
    end dates across the columns. Pure, so it can be tested without the network.
    """
    import pandas as pd
    by_end: dict[date, FinancialPeriod] = {}
    for frame in frames:
        if frame is None or not hasattr(frame, "empty") or frame.empty:
            continue
        labels = {str(i).strip(): i for i in frame.index}
        for column in frame.columns:
            try:
                end = pd.Timestamp(column).date()
            except Exception:
                continue
            period = by_end.get(end)
            if period is None:
                period = by_end[end] = FinancialPeriod(
                    period_end=end, period_type=period_type, as_reported_at=None,
                    currency=currency, source=source, quality=UNVERIFIED)
            for item, candidates in YahooProvider._STATEMENT_ROWS.items():
                if item in period.values:
                    continue
                for label in candidates:
                    if label not in labels:
                        continue
                    value = frame.loc[labels[label], column]
                    if value is None or pd.isna(value):
                        continue
                    period.values[item] = float(value)
                    break
    return [p for p in by_end.values() if p.values]
