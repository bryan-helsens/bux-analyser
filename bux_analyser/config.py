"""Runtime configuration. Everything is local; paths can be overridden with env vars."""
from __future__ import annotations

import os
from pathlib import Path

BASE_CURRENCY = "EUR"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BUX_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = Path(os.environ.get("BUX_DB_PATH", DATA_DIR / "bux_analyser.db"))
RAW_DIR = DATA_DIR / "raw"  # archived copies of imported files

for _p in (DATA_DIR, RAW_DIR):
    _p.mkdir(parents=True, exist_ok=True)


from dataclasses import dataclass


@dataclass(frozen=True)
class Benchmark:
    label: str
    ticker: str
    total_return: bool   # accumulating ETF (dividends included) vs a price-only index
    note: str

    @property
    def key(self) -> str:
        return f"BM:{self.ticker}"


# EUR-listed accumulating UCITS ETFs are used as benchmark proxies wherever possible:
# their price series is already in EUR and already includes reinvested dividends, so it
# compares like for like with a EUR investor's total return. A price-only index is
# flagged so the dashboard can say the comparison understates the benchmark.
BENCHMARKS: tuple[Benchmark, ...] = (
    Benchmark("MSCI World", "IWDA.AS", True, "iShares Core MSCI World, EUR, accumulating"),
    Benchmark("FTSE All-World", "VWCE.DE", True, "Vanguard FTSE All-World, EUR, accumulating"),
    Benchmark("S&P 500", "SXR8.DE", True, "iShares Core S&P 500, EUR, accumulating"),
    Benchmark("Nasdaq 100", "SXRV.DE", True, "iShares Nasdaq 100, EUR, accumulating"),
    Benchmark("AEX", "^AEX", False, "AEX price index: excludes dividends, so it understates the real return"),
)
BENCHMARK_BY_KEY = {b.key: b for b in BENCHMARKS}
DEFAULT_BENCHMARK = BENCHMARKS[0].key
