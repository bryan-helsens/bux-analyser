"""SEC EDGAR company facts: free, official, and the only source here that publishes a
filing date alongside every figure.

Covers US filers, including foreign companies that file a 20-F. The parsing is a pure
function so it can be tested against a recorded response rather than the live service.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import requests

from .base import Provenance, ProviderHealth, SecurityIds
from .fundamentals import FY, Q, REPORTED, FinancialPeriod, Fundamentals

log = logging.getLogger(__name__)

COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# Each line item lists the XBRL concepts that may carry it, best first. Companies tag
# the same economic quantity differently, so a single concept name is never enough.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "eps_diluted": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "total_debt": ("LongTermDebt", "LongTermDebtNoncurrent", "DebtLongtermAndShorttermCombinedAmount"),
    "total_equity": ("StockholdersEquity",
                     "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "total_assets": ("Assets",),
    "shares_diluted": ("WeightedAverageNumberOfDilutedSharesOutstanding",
                       "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted"),
    "dividends_paid": ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"),
}
ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
QUARTERLY_FORMS = {"10-Q"}


def _as_date(value: str | None) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except (TypeError, ValueError):
        return None


def parse_company_facts(payload: dict[str, Any], isin: str, ticker: str | None = None,
                        source: str = "SEC EDGAR") -> Fundamentals:
    """Turn a companyfacts document into periods keyed by fiscal year end.

    Only figures whose form is an annual or quarterly report are kept, and every one
    carries the date it was filed. Where a company restates a period in a later filing,
    the earliest filing of that value is kept, because that is what was knowable at the
    time.
    """
    facts = (payload or {}).get("facts", {})
    currency = None
    buckets: dict[tuple[date, str], FinancialPeriod] = {}

    for item, concepts in CONCEPTS.items():
        for taxonomy in ("us-gaap", "ifrs-full"):
            found = False
            for concept in concepts:
                node = facts.get(taxonomy, {}).get(concept)
                if not node:
                    continue
                for unit, entries in (node.get("units") or {}).items():
                    if unit not in ("USD", "EUR", "GBP", "shares", "USD/shares", "EUR/shares"):
                        continue
                    if unit in ("USD", "EUR", "GBP") and currency is None:
                        currency = unit
                    for entry in entries:
                        form = entry.get("form")
                        period_type = FY if form in ANNUAL_FORMS else (Q if form in QUARTERLY_FORMS else None)
                        if period_type is None:
                            continue
                        end, filed = _as_date(entry.get("end")), _as_date(entry.get("filed"))
                        value = entry.get("val")
                        if end is None or value is None:
                            continue
                        key = (end, period_type)
                        period = buckets.get(key)
                        if period is None:
                            period = buckets[key] = FinancialPeriod(
                                period_end=end, period_type=period_type, as_reported_at=filed,
                                currency=currency, source=source, quality=REPORTED)
                        if item in period.values:
                            continue                      # first filing wins over restatements
                        period.values[item] = float(value)
                        if filed and (period.as_reported_at is None or filed < period.as_reported_at):
                            period.as_reported_at = filed
                    found = True
                if found:
                    break
            if found:
                break

    periods = [p for p in buckets.values() if p.values]
    for p in periods:
        p.currency = p.currency or currency
    return Fundamentals(isin=isin, ticker=ticker, currency=currency,
                        periods=sorted(periods, key=lambda p: (p.period_end, p.period_type)),
                        provenance=Provenance("edgar", "SEC EDGAR company facts (XBRL)",
                                              Provenance.now(),
                                              max((p.as_reported_at for p in periods if p.as_reported_at),
                                                  default=None),
                                              REPORTED, "official filings"))


class EdgarProvider:
    """US company fundamentals. Free, no key, but the SEC requires a contact header
    and fair-use rate limiting."""
    name = "edgar"
    cost = "free"
    source = "SEC EDGAR company facts (XBRL)"

    def __init__(self, contact: str = "bux-analyser (personal use)", timeout: float = 30.0):
        self.headers = {"User-Agent": contact, "Accept-Encoding": "gzip, deflate"}
        self.timeout = timeout
        self.health = ProviderHealth(self.name)
        self._cik_map: dict[str, int] | None = None

    def _tickers(self) -> dict[str, int]:
        if self._cik_map is None:
            try:
                r = requests.get(COMPANY_TICKERS, headers=self.headers, timeout=self.timeout)
                r.raise_for_status()
                self._cik_map = {row["ticker"].upper(): int(row["cik_str"])
                                 for row in r.json().values()}
                self.health.ok()
            except Exception as e:
                self.health.fail(e)
                self._cik_map = {}
        return self._cik_map

    def cik_for(self, ticker: str | None) -> int | None:
        if not ticker:
            return None
        # strip an exchange suffix such as .AS or .DE; EDGAR knows only the US symbol
        base = ticker.split(".")[0].upper()
        return self._tickers().get(base)

    def fundamentals(self, ids: SecurityIds) -> Fundamentals | None:
        cik = self.cik_for(ids.ticker)
        if cik is None:
            return None                       # not a US filer: expected for most EU holdings
        try:
            r = requests.get(COMPANY_FACTS.format(cik=cik), headers=self.headers, timeout=self.timeout)
            if r.status_code == 404:
                self.health.fail(f"no company facts for CIK {cik}")
                return None
            r.raise_for_status()
            data = parse_company_facts(r.json(), ids.isin, ids.ticker, self.source)
            self.health.ok()
            return data if not data.is_empty else None
        except Exception as e:
            self.health.fail(e)
            return None
