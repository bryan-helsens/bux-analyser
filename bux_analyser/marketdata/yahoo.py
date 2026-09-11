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
    def resolve(self, isin: str, hint_name: str | None = None, hint_currency: str | None = None) -> SecurityIds | None:
        import yfinance as yf
        self._throttle()
        try:
            candidates = []
            try:
                res = yf.Search(isin, max_results=10).quotes or []
                candidates = [q for q in res if q.get("symbol")]
            except Exception as e:  # search endpoint is the flakiest part of Yahoo
                log.info("yahoo search by ISIN failed for %s: %s", isin, e)
            if not candidates:
                t = yf.Ticker(isin)  # Yahoo often accepts an ISIN directly as a symbol
                info = t.fast_info
                sym = getattr(info, "symbol", None) or (t.info or {}).get("symbol")
                if sym:
                    candidates = [{"symbol": sym, "exchange": (t.info or {}).get("exchange"), "shortname": (t.info or {}).get("shortName")}]
            if not candidates:
                self.health.fail(f"no symbol for {isin}")
                return None
            if hint_currency:
                # keep candidates that quote in the broker's trading currency when we can tell
                filtered = []
                for c in candidates:
                    try:
                        ccy = yf.Ticker(c["symbol"]).fast_info.currency
                    except Exception:
                        ccy = None
                    if ccy is None or ccy.upper() == hint_currency.upper():
                        filtered.append(c)
                    self._throttle()
                candidates = filtered or candidates
            def rank(c):
                ex = c.get("exchange") or ""
                return _EXCHANGE_PREFERENCE.index(ex) if ex in _EXCHANGE_PREFERENCE else 99
            best = sorted(candidates, key=rank)[0]
            sym = best["symbol"]
            ccy = hint_currency
            try:
                ccy = yf.Ticker(sym).fast_info.currency or ccy
            except Exception:
                pass
            self.health.ok()
            return SecurityIds(isin=isin, ticker=sym, currency=(ccy or "").upper() or None,
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
