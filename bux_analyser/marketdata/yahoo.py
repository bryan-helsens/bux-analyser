"""Yahoo Finance via yfinance. Free, unofficial; personal non-commercial use.

Design for breakage: every call is wrapped, throttled, and returns None on failure so the
router/cache can degrade gracefully. Never raises into the application.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd

from .base import PriceSeries, Provenance, ProviderHealth, Quote, SecurityIds

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
