import numpy as np
import pandas as pd
import pytest

from bux_analyser.analytics.factors import FACTOR_LABELS, parse_french_csv, regress

DAYS = pd.date_range("2022-01-03", periods=600, freq="B")


def factor_frame(seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "Mkt-RF": rng.normal(0.0003, 0.010, len(DAYS)),
        "SMB": rng.normal(0.0, 0.004, len(DAYS)),
        "HML": rng.normal(0.0, 0.005, len(DAYS)),
    }, index=DAYS)


def test_regression_recovers_known_loadings():
    f = factor_frame()
    truth = {"Mkt-RF": 1.2, "SMB": -0.4, "HML": 0.6}
    y = sum(f[k] * v for k, v in truth.items())      # a portfolio built exactly from factors
    result = regress(y, f)
    for key, expected in truth.items():
        assert result.loadings[key] == pytest.approx(expected, abs=1e-6)
    assert result.r_squared == pytest.approx(1.0)
    assert result.alpha_annual == pytest.approx(0.0, abs=1e-6)


def test_noise_produces_loadings_that_are_not_significant():
    f = factor_frame()
    rng = np.random.default_rng(99)
    y = pd.Series(rng.normal(0, 0.01, len(DAYS)), index=DAYS)
    result = regress(y, f)
    assert all(abs(t) < 3 for t in result.t_statistics.values())
    assert "No factor tilt stands out" in " ".join(result.describe())


def test_description_only_mentions_tilts_it_can_distinguish():
    f = factor_frame()
    rng = np.random.default_rng(5)
    y = f["Mkt-RF"] * 1.1 + f["HML"] * 0.8 + pd.Series(rng.normal(0, 0.002, len(DAYS)), index=DAYS)
    result = regress(y, f)
    joined = " ".join(result.describe())
    assert "times with the market" in joined
    assert result.loadings["Mkt-RF"] == pytest.approx(1.1, abs=0.05)
    assert FACTOR_LABELS["HML"].lower() in joined.lower()
    # the factor it has no real exposure to is left out of the description
    assert FACTOR_LABELS["SMB"].lower() not in joined.lower()


def test_alpha_near_zero_is_called_out_as_the_normal_result():
    f = factor_frame()
    y = f["Mkt-RF"] * 1.0 + pd.Series(np.random.default_rng(1).normal(0, 0.003, len(DAYS)), index=DAYS)
    result = regress(y, f)
    assert any("not distinguishable from zero" in n for n in result.notes)


def test_regression_refuses_on_too_little_overlap():
    f = factor_frame()
    short = pd.Series(np.zeros(10), index=DAYS[:10])
    assert regress(short, f) is None
    assert regress(pd.Series(dtype=float), f) is None
    assert regress(f["Mkt-RF"], pd.DataFrame()) is None


def test_french_file_parsing_skips_prose_and_the_annual_table():
    text = "\n".join([
        "This file was created using the 202606 CRSP database.",
        "Missing data are indicated by -99.99.",
        "",
        ",Mkt-RF,SMB,HML,RF",
        "20240102,  0.50, -0.10,  0.20, 0.02",
        "20240103, -0.30,  0.05, -0.15, 0.02",
        "",
        "  Annual Factors: January-December",
        ",Mkt-RF,SMB,HML,RF",
        "2024, 10.50, -2.10,  4.20, 0.50",
    ])
    frame = parse_french_csv(text)
    assert list(frame.columns) == ["Mkt-RF", "SMB", "HML", "RF"]
    assert len(frame) == 2                       # the annual rows are not daily data
    assert frame.loc[pd.Timestamp("2024-01-02"), "Mkt-RF"] == pytest.approx(0.005)


def test_unparseable_factor_file_returns_nothing():
    assert parse_french_csv("no table here at all").empty
    assert parse_french_csv("").empty
