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
