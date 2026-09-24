from news_sentiment.news_sentiment_scraper import build_matcher, find_tickers
from news_sentiment.suggestions import build_suggestions, clean_company_name, company_aliases, stock_signal

MARKET = [
    {"ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology", "market_cap": 4e12, "current_price": 200.0, "day_change_pct": 1.5, "momentum_pct": 12.0},
    {"ticker": "MSFT", "name": "Microsoft Corporation", "sector": "Technology", "market_cap": 3e12, "current_price": 500.0, "day_change_pct": -0.5, "momentum_pct": 8.0},
    {"ticker": "XOM", "name": "Exxon Mobil Corporation", "sector": "Energy", "market_cap": 5e11, "current_price": 110.0, "day_change_pct": -2.0, "momentum_pct": -6.0},
    {"ticker": "SMLL", "name": "Small Energy Co", "sector": "Energy", "market_cap": 1e9, "current_price": 5.0, "day_change_pct": 4.0, "momentum_pct": 90.0},
    {"ticker": "BRK-B", "name": "Berkshire Hathaway Inc.", "sector": "Unknown", "market_cap": 1e12, "current_price": 500.0, "day_change_pct": 0.1, "momentum_pct": 3.0},
    {"ticker": "TGT", "name": "Target Corporation", "sector": "Consumer Discretionary", "market_cap": 5e10, "current_price": 90.0, "day_change_pct": 0.0, "momentum_pct": 1.0},
]
LOOKUP = {r["ticker"]: r for r in MARKET}.get


def article(title, sentiment, tickers="", sectors="", ts="2026-09-23T10:00:00+00:00"):
    return {"title": title, "sentiment": sentiment, "tickers": tickers, "sectors_affected": sectors,
            "published_ts": ts, "link": "https://e.com", "source": "Test", "published": ts}


def test_clean_company_name_strips_legal_suffixes():
    assert clean_company_name("Eli Lilly and Company") == "Eli Lilly"
    assert clean_company_name("Amazon.com, Inc.") == "Amazon"
    assert clean_company_name("Toronto Dominion Bank (The)") == "Toronto Dominion Bank"
    assert clean_company_name("Meta Platforms Inc. Class A") == "Meta Platforms"


def test_headline_matching_is_case_sensitive_and_skips_everyday_words():
    names = company_aliases(MARKET)
    matcher = build_matcher(names, known_tickers={r["ticker"] for r in MARKET})

    assert find_tickers("Apple and Microsoft rally", matcher) == {"AAPL": "Apple", "MSFT": "Microsoft"}
    assert find_tickers("An apple a day", matcher) == {}
    assert find_tickers("Analyst raises price Target", matcher) == {}      # "Target" is an everyday word
    assert find_tickers("Target (NYSE: TGT) cuts guidance", matcher) == {"TGT": "TGT"}
    assert find_tickers("Buffett buys more (NYSE:BRK.B) and $FAKE", matcher) == {"BRK-B": "BRK-B"}


def test_suggestions_pair_news_with_present_condition():
    results = [
        article("Apple beats", "Positive", "AAPL", ts="2026-09-23T09:00:00+00:00"),
        article("Apple launches phone", "Positive", "AAPL", ts="2026-09-23T11:00:00+00:00"),
        article("Microsoft outage", "Negative", "MSFT"),
        article("Oil slides on supply", "Negative", sectors="Energy"),
        article("Berkshire buys", "Neutral", "BRK-B"),
        article("Unparsed", "Unknown", "AAPL"),
    ]

    s = build_suggestions(results, LOOKUP, MARKET)

    aapl, msft = s["stocks"][0], next(x for x in s["stocks"] if x["ticker"] == "MSFT")
    assert (aapl["ticker"], aapl["mentions"], aapl["tone"], aapl["tone_label"]) == ("AAPL", 2, 1.0, "Positive")
    assert aapl["latest_headline"]["title"] == "Apple launches phone"
    assert aapl["condition"] == {"current_price": 200.0, "day_change_pct": 1.5, "momentum_pct": 12.0, "relative_strength": None}
    assert aapl["signal"] == "Good news on a rising trend"
    assert msft["signal"] == "Bad news on a rising trend: watch closely"

    sectors = {x["sector"]: x for x in s["sectors"]}
    assert set(sectors) == {"Technology", "Energy"}                   # "Unknown" isn't a sector to suggest
    assert sectors["Technology"]["mentions"] == 3
    energy = sectors["Energy"]["condition"]
    assert energy["median_day_change_pct"] == 1.0                     # median of -2.0 and 4.0
    assert energy["advancing_pct"] == 50.0
    assert [l["ticker"] for l in energy["leaders"]] == ["XOM"]         # $10B+ only
    assert s["overview"] == {"headlines": 5, "positive": 2, "negative": 2, "neutral": 1,
                             "stocks_mentioned": 3, "sectors_mentioned": 2}


def test_stock_signal_wording():
    assert stock_signal(0.5, -10, 0) == "Good news on a falling trend: watch for a turn"
    assert stock_signal(-0.5, -10, 0) == "Bad news on a falling trend: caution"
    assert stock_signal(0.0, 5, -4.2) == "In the news and moving down sharply today"
    assert stock_signal(0.0, 5, 0.3) == "In the news, no clear direction"
