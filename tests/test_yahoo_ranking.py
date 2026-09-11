from bux_analyser.marketdata.yahoo import YahooProvider


def test_prefers_trading_currency_then_exchange():
    cands = [
        {"symbol": "NVDA", "exchange": "NMS", "currency": "USD", "quoteType": "EQUITY"},
        {"symbol": "NVD.DE", "exchange": "GER", "currency": "EUR", "quoteType": "EQUITY"},
        {"symbol": "NVD.F", "exchange": "FRA", "currency": "EUR", "quoteType": "EQUITY"},
        {"symbol": "NVDA.MX", "exchange": "MEX", "currency": "MXN", "quoteType": "EQUITY"},
    ]
    assert YahooProvider.rank_candidates(cands, "EUR")[0]["symbol"] == "NVD.DE"
    assert YahooProvider.rank_candidates(cands, "USD")[0]["symbol"] == "NVDA"
    # no hint: exchange preference decides
    assert YahooProvider.rank_candidates(cands, None)[0]["symbol"] == "NVD.DE"


def test_unknown_currency_ranks_between_match_and_mismatch():
    cands = [{"symbol": "A", "exchange": "NYQ", "currency": "USD"}, {"symbol": "B", "exchange": "AMS", "currency": None}]
    assert YahooProvider.rank_candidates(cands, "EUR")[0]["symbol"] == "B"
