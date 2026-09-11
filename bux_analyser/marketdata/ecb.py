"""ECB reference rates via the Frankfurter API (free, no key, open-source mirror of ECB data)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import requests

from .base import FxSeries, Provenance, ProviderHealth

FRANKFURTER = "https://api.frankfurter.dev/v1"


class ECBProvider:
    name = "ecb"
    cost = "free"
    source = "ECB reference rates (via frankfurter.dev)"

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.health = ProviderHealth(self.name)

    def fx_history(self, currency: str, base: str, start: date, end: date) -> FxSeries | None:
        """Returns base per 1 unit of `currency` (e.g. EUR per USD)."""
        if currency == base:
            idx = pd.date_range(start, end, freq="D")
            return FxSeries(pd.Series(1.0, index=idx), currency, base,
                            Provenance(self.name, "identity", Provenance.now(), end, "calculated"))
        try:
            url = f"{FRANKFURTER}/{start:%Y-%m-%d}..{end:%Y-%m-%d}"
            r = requests.get(url, params={"base": currency, "symbols": base}, timeout=self.timeout)
            r.raise_for_status()
            rates = r.json().get("rates", {})
            if not rates:
                self.health.fail(f"no rates {currency}->{base}")
                return None
            s = pd.Series({pd.Timestamp(d): float(v[base]) for d, v in rates.items()}).sort_index()
            self.health.ok()
            return FxSeries(s, currency, base,
                            Provenance(self.name, self.source, Provenance.now(), s.index[-1].date(), "reported"))
        except Exception as e:
            self.health.fail(e)
            return None
