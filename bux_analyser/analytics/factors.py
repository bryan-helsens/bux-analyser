"""Factor analysis: what kind of bets a portfolio is actually making.

A portfolio's returns can be largely explained by a handful of well-documented
exposures: the market itself, company size, value against growth, profitability and
investment, plus momentum. Regressing your returns on those says whether you are taking
a distinctive bet or an expensive index.

This is descriptive, not predictive. A loading tells you what your portfolio has behaved
like; it does not say what it will earn. Factor returns come from the Kenneth French
data library, which is free and academic.
"""
from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

FRENCH_BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
DATASETS = {
    "developed": "Developed_5_Factors_Daily_CSV.zip",
    "europe": "Europe_5_Factors_Daily_CSV.zip",
    "us": "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
}
FACTOR_LABELS = {
    "Mkt-RF": "Market", "SMB": "Smaller companies", "HML": "Value over growth",
    "RMW": "Profitability", "CMA": "Conservative investment", "Mom": "Momentum",
}
MIN_OBS = 60


@dataclass
class FactorExposure:
    loadings: dict[str, float] = field(default_factory=dict)
    t_statistics: dict[str, float] = field(default_factory=dict)
    alpha_annual: float | None = None
    alpha_t: float | None = None
    r_squared: float | None = None
    n_obs: int = 0
    dataset: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def reliable(self) -> bool:
        return self.n_obs >= 252 and (self.r_squared or 0) > 0.3

    def describe(self) -> list[str]:
        """Plain sentences about the tilts that are actually distinguishable from noise."""
        out = []
        for key, loading in sorted(self.loadings.items(), key=lambda kv: -abs(kv[1])):
            t = self.t_statistics.get(key, 0.0)
            if abs(t) < 2.0:
                continue                      # not distinguishable from zero
            label = FACTOR_LABELS.get(key, key)
            if key == "Mkt-RF":
                out.append(f"Moves about {loading:.2f} times with the market.")
            else:
                direction = "tilted towards" if loading > 0 else "tilted away from"
                out.append(f"{direction.capitalize()} {label.lower()} ({loading:+.2f}).")
        if not out:
            out.append("No factor tilt stands out from the noise over this period.")
        return out


def regress(portfolio_excess: pd.Series, factors: pd.DataFrame,
            dataset: str = "") -> FactorExposure | None:
    """Ordinary least squares of excess returns on factor returns.

    Returns None rather than a fragile answer when there is too little overlap, and
    reports a t-statistic for every loading so a tilt can be told from noise.
    """
    if portfolio_excess is None or factors is None or factors.empty:
        return None
    df = pd.concat([portfolio_excess.rename("y"), factors], axis=1, sort=True).dropna()
    if len(df) < MIN_OBS:
        return None
    y = df["y"].to_numpy(dtype=float)
    names = [c for c in factors.columns if c in df.columns]
    X = np.column_stack([np.ones(len(df))] + [df[c].to_numpy(dtype=float) for c in names])
    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    residuals = y - X @ beta
    dof = len(df) - X.shape[1]
    if dof <= 0:
        return None
    sigma2 = float(residuals @ residuals) / dof
    try:
        covariance = sigma2 * np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return None
    errors = np.sqrt(np.clip(np.diag(covariance), 0, None))
    total = float(((y - y.mean()) ** 2).sum())
    result = FactorExposure(
        loadings={name: float(beta[i + 1]) for i, name in enumerate(names)},
        t_statistics={name: (float(beta[i + 1] / errors[i + 1]) if errors[i + 1] > 0 else 0.0)
                      for i, name in enumerate(names)},
        alpha_annual=float((1 + beta[0]) ** 252 - 1),
        alpha_t=float(beta[0] / errors[0]) if errors[0] > 0 else 0.0,
        r_squared=(1 - float(residuals @ residuals) / total) if total > 0 else None,
        n_obs=len(df), dataset=dataset)
    if not result.reliable:
        result.notes.append(
            f"Based on {result.n_obs} days with an R-squared of "
            f"{(result.r_squared or 0):.0%}; treat the loadings as rough.")
    if result.alpha_t is not None and abs(result.alpha_t) < 2:
        result.notes.append("Alpha is not distinguishable from zero, which is the usual result "
                            "and the one to expect.")
    return result


def parse_french_csv(text: str) -> pd.DataFrame:
    """Read a Kenneth French daily factor file.

    The files carry a prose header, then a dated table, then sometimes a second annual
    table below. Values are percentages.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.split(",")]
        if len(cells) >= 4 and cells[0] == "" and any(c in FACTOR_LABELS for c in cells):
            start = i
            break
    if start is None:
        return pd.DataFrame()
    rows, columns = [], [c.strip() for c in lines[start].split(",")][1:]
    for line in lines[start + 1:]:
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(columns) + 1 or not cells[0].isdigit() or len(cells[0]) != 8:
            if rows:
                break                      # the daily table has ended
            continue
        try:
            rows.append([pd.Timestamp(cells[0])] + [float(c) / 100.0 for c in cells[1:]])
        except ValueError:
            continue
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=["date"] + columns).set_index("date")
    return frame


class FrenchFactorProvider:
    """Downloads the factor files. Free and unauthenticated, updated periodically."""
    name = "french"
    cost = "free"
    source = "Kenneth French data library"

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout
        self._cache: dict[str, pd.DataFrame] = {}

    def factors(self, dataset: str = "developed") -> pd.DataFrame | None:
        if dataset in self._cache:
            return self._cache[dataset]
        filename = DATASETS.get(dataset)
        if filename is None:
            return None
        try:
            r = requests.get(FRENCH_BASE + filename, timeout=self.timeout,
                             headers={"User-Agent": "bux-analyser (personal use)"})
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
                name = archive.namelist()[0]
                text = archive.read(name).decode("latin-1")
            frame = parse_french_csv(text)
            if frame.empty:
                return None
            self._cache[dataset] = frame
            return frame
        except Exception as e:
            log.info("french factor download failed: %s", e)
            return None
