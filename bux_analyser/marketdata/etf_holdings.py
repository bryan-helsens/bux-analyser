"""ETF constituents, taken from the files issuers publish themselves.

There is no free API for UCITS ETF holdings, but every large issuer publishes a
holdings file on each fund's page. Parsing those is the only honest way to say what an
ETF actually exposes you to. The parsers are pure functions over file text so they can
be tested against recorded files; the downloader is a thin wrapper around them.

Coverage is uneven by design: iShares publishes daily CSV at a predictable address,
Vanguard monthly, and some issuers only a PDF factsheet. A fund we cannot parse stays
visible as an unbroken bucket rather than being quietly dropped or guessed at.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import requests

from .base import Provenance, ProviderHealth

log = logging.getLogger(__name__)
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


@dataclass
class Constituent:
    identifier: str                 # ISIN where the issuer publishes one, else a ticker
    name: str | None = None
    weight: float = 0.0             # fraction of the fund, not a percentage
    sector: str | None = None
    country: str | None = None
    currency: str | None = None
    asset_class: str | None = None

    @property
    def is_isin(self) -> bool:
        return bool(_ISIN.match(self.identifier or ""))


@dataclass
class HoldingsFile:
    etf_isin: str
    as_of: date
    constituents: list[Constituent] = field(default_factory=list)
    source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def total_weight(self) -> float:
        return sum(c.weight for c in self.constituents)

    @property
    def is_complete(self) -> bool:
        """Issuers round weights, so anything within a couple of points is the whole fund."""
        return 0.97 <= self.total_weight <= 1.03


# Column names differ by issuer but mean the same thing.
ALIASES = {
    # ISIN first because it matches our own holdings; ticker next because it at least
    # resolves to a quote; SEDOL last because it is proprietary and matches nothing here.
    "identifier": ("isin", "security isin", "ticker", "holding ticker", "identifier", "sedol"),
    "name": ("name", "security name", "issuer name", "holding name", "description"),
    "weight": ("weight (%)", "weight", "% of net assets", "percent of fund", "weighting",
               "% of fund", "portfolio weight", "% weight"),
    "sector": ("sector", "gics sector", "industry"),
    "country": ("location", "country", "domicile", "region"),
    "currency": ("currency", "market currency", "trading currency"),
    "asset_class": ("asset class", "asset type", "security type"),
}


def _normalise(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _number(raw: str) -> float | None:
    s = re.sub(r"[^0-9,.\-]", "", (raw or "").strip())
    if not s or s in ("-", "."):
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rindex(".") > s.rindex(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head}.{tail}" if len(tail) != 3 else head + tail
    try:
        return float(s)
    except ValueError:
        return None


def _find_header(lines: list[str]) -> int | None:
    """Issuer files carry a preamble of fund name, date and disclaimers before the table."""
    for i, line in enumerate(lines[:40]):
        cells = {_normalise(c) for c in next(csv.reader([line]), [])}
        if cells & set(ALIASES["weight"]) and (cells & set(ALIASES["identifier"]) or
                                               cells & set(ALIASES["name"])):
            return i
    return None


def _find_date(lines: list[str]) -> date | None:
    for line in lines[:20]:
        for pattern, fmt in (("(\\d{2}/[A-Za-z]{3}/\\d{4})", "%d/%b/%Y"),
                             ("(\\d{4}-\\d{2}-\\d{2})", "%Y-%m-%d"),
                             ("([A-Z][a-z]{2} \\d{1,2}, \\d{4})", "%b %d, %Y"),
                             ("(\\d{2}/\\d{2}/\\d{4})", "%m/%d/%Y")):
            m = re.search(pattern, line)
            if m:
                try:
                    return datetime.strptime(m.group(1), fmt).date()
                except ValueError:
                    continue
    return None


def parse_holdings_csv(text: str, etf_isin: str, source: str = "issuer file",
                       as_of: date | None = None) -> HoldingsFile | None:
    """Read an issuer holdings file.

    Works across issuers by matching column meaning rather than exact position, because
    iShares, Vanguard, SPDR and Invesco all name the same columns differently.
    """
    if not text or not text.strip():
        return None
    lines = text.splitlines()
    header_row = _find_header(lines)
    if header_row is None:
        return None
    file_date = as_of or _find_date(lines[:header_row] or lines) or date.today()
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_row:])))
    # Match by alias preference, not by column order: an iShares file carries both
    # "Ticker" and "ISIN", and the ISIN is the one that identifies a company unambiguously.
    columns = {_normalise(c): c for c in (reader.fieldnames or [])}
    lookup: dict[str, str] = {}
    for field_name, options in ALIASES.items():
        for alias in options:
            if alias in columns:
                lookup[field_name] = columns[alias]
                break

    out = HoldingsFile(etf_isin=etf_isin, as_of=file_date, source=source)
    percent_scale = False
    rows = list(reader)
    raw_weights = [_number(r.get(lookup.get("weight", ""), "")) for r in rows]
    usable = [w for w in raw_weights if w is not None]
    if usable and (sum(usable) > 2.0 or max(usable) > 1.5):
        percent_scale = True          # weights published as percentages, not fractions

    for row, weight in zip(rows, raw_weights):
        if weight is None:
            continue
        identifier = (row.get(lookup.get("identifier", ""), "") or "").strip()
        name = (row.get(lookup.get("name", ""), "") or "").strip() or None
        if not identifier and not name:
            continue
        out.constituents.append(Constituent(
            identifier=identifier or name,
            name=name,
            weight=weight / 100.0 if percent_scale else weight,
            sector=(row.get(lookup.get("sector", ""), "") or "").strip() or None,
            country=(row.get(lookup.get("country", ""), "") or "").strip() or None,
            currency=(row.get(lookup.get("currency", ""), "") or "").strip() or None,
            asset_class=(row.get(lookup.get("asset_class", ""), "") or "").strip() or None))

    if not out.constituents:
        return None
    if not out.is_complete:
        out.notes.append(f"Weights add to {out.total_weight:.1%}, not 100%. The file may be "
                         "partial, or it may exclude cash and derivatives.")
    if not any(c.is_isin for c in out.constituents):  # noqa: SIM102 - explicit for clarity
        out.notes.append("This issuer does not publish ISINs, so constituents are matched by "
                         "ticker or name and may not line up with your own holdings.")
    return out


# Known issuer file locations. Each is the address the fund page itself links to.
ISSUER_URLS: dict[str, str] = {
    # iShares publishes a stable ajax endpoint per product, daily.
    "IE00B4L5Y983": "https://www.ishares.com/nl/particuliere-belegger/nl/producten/251882/"
                    "ishares-msci-world-ucits-etf-acc-fund/1506575576011.ajax"
                    "?fileType=csv&fileName=IWDA_holdings&dataType=fund",
}


class IssuerHoldingsProvider:
    """Downloads and parses issuer holdings files.

    Registered addresses are the ones published on each fund's own page. Nothing is
    scraped: these are the files the issuer offers for download.
    """
    name = "issuer"
    cost = "free"
    source = "issuer holdings file"

    def __init__(self, urls: dict[str, str] | None = None, timeout: float = 30.0):
        self.urls = {**ISSUER_URLS, **(urls or {})}
        self.timeout = timeout
        self.health = ProviderHealth(self.name)

    def register(self, etf_isin: str, url: str) -> None:
        """Let the user paste the download address from a fund page we do not know."""
        self.urls[etf_isin] = url

    def holdings(self, etf_isin: str) -> HoldingsFile | None:
        url = self.urls.get(etf_isin)
        if not url:
            return None
        try:
            r = requests.get(url, timeout=self.timeout,
                             headers={"User-Agent": "Mozilla/5.0 (bux-analyser, personal use)"})
            r.raise_for_status()
            parsed = parse_holdings_csv(r.text, etf_isin, f"{self.source} ({url.split('/')[2]})")
            if parsed is None:
                self.health.fail(f"could not parse the holdings file for {etf_isin}")
                return None
            self.health.ok()
            return parsed
        except Exception as e:
            self.health.fail(e)
            return None

    def provenance(self, file: HoldingsFile) -> Provenance:
        return Provenance(self.name, file.source, Provenance.now(), file.as_of, "reported",
                          "published by the fund issuer")
