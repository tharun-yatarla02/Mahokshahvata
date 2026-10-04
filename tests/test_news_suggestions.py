from news_sentiment.news_sentiment_scraper import build_matcher, find_macro_themes, find_tickers
from news_sentiment.suggestions import (
    affected_stocks, build_suggestions, clean_company_name, company_aliases, largest_by_sector, stock_signal,
)

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


def test_clean_company_name_strips_listing_details_and_skips_debt():
    assert clean_company_name("SAP  SE ADS") == "SAP"
    assert clean_company_name("Brookfield Corporation Class A Limited Voting Shares") == "Brookfield"
    assert clean_company_name("Duke Energy Corporation (Holding Company)") == "Duke Energy"
    assert clean_company_name("MPLX LP Common Units Representing Limited Partner Interests") == "MPLX"
    assert clean_company_name("America Movil S.A.B. de C.V.") == "America Movil"
    assert clean_company_name("Iron Mountain Incorporated (Delaware)Common Stock REIT") == "Iron Mountain"
    assert clean_company_name("Entergy Arkansas LLC First Mortgage Bonds 4.875% Series Due September 1 2066") == ""
    assert clean_company_name("Willis Towers Watson Public Limited Company") == "Willis Towers Watson"
    assert clean_company_name("Comcast Holdings ZONES") == ""
    assert clean_company_name("Brookfield Renewable Corporation Brookfield Renewable Corporation "
                              "Class A Exchangeable Subordinate Voting Shares") == "Brookfield Renewable"


def test_headline_matching_is_case_sensitive_and_skips_everyday_words():
    names = company_aliases(MARKET)
    matcher = build_matcher(names, known_tickers={r["ticker"] for r in MARKET})

    assert find_tickers("Apple and Microsoft rally", matcher) == {"AAPL": "Apple", "MSFT": "Microsoft"}
    assert find_tickers("An apple a day", matcher) == {}
    assert find_tickers("Analyst raises price Target", matcher) == {}      # "Target" is an everyday word
    assert find_tickers("Target (NYSE: TGT) cuts guidance", matcher) == {"TGT": "TGT"}
    assert find_tickers("Buffett buys more (NYSE:BRK.B) and $FAKE", matcher) == {"BRK-B": "BRK-B"}


def test_bare_ticker_in_brackets_after_a_name():
    matcher = build_matcher([("Ford", "F")], known_tickers={"CHWY", "WOOF", "AI", "F"})
    assert find_tickers("Chewy (CHWY) vs. Petco (WOOF): Which to Buy?", matcher) == {"CHWY": "CHWY", "WOOF": "WOOF"}
    assert find_tickers("Startup Zeta (ZETA) soars", matcher) == {}                   # not a tracked symbol
    assert find_tickers("Firms adopt artificial intelligence (AI)", matcher) == {}    # lowercase before it
    assert find_tickers("Generative AI (AI) spending rises", matcher) == {}           # common abbreviation
    assert find_tickers("Ford recalls SUVs", matcher) == {"F": "Ford"}
    assert find_tickers("Harrison Ford and Gerald Ford", matcher) == {}               # people, not the company
    assert find_tickers("Ford (CHWY)", build_matcher([], known_tickers=None)) == {}   # bare form needs real symbols


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


def test_each_headline_lists_the_stocks_it_affects_with_a_verdict():
    by_sector = largest_by_sector(MARKET)
    view = lambda a: [(s["ticker"], s["via"], s["view"]) for s in affected_stocks(a, LOOKUP, by_sector)]

    # Named stocks: the article's tone plus each stock's 90-day trend.
    assert view(article("Apple beats", "Positive", tickers="AAPL")) == [("AAPL", None, "Good")]
    assert view(article("Exxon slumps", "Negative", tickers="XOM")) == [("XOM", None, "Bad")]
    assert view(article("Exxon upgraded", "Positive", tickers="XOM")) == [("XOM", None, "Wait")]   # good news, falling trend
    assert view(article("Apple sued", "Negative", tickers="AAPL")) == [("AAPL", None, "Wait")]     # bad news, rising trend
    assert view(article("Apple event", "Neutral", tickers="AAPL")) == [("AAPL", None, "No clear signal")]

    # Sector-wide news: the largest companies, not the best performers (SMLL is up 90%).
    assert view(article("Oil prices jump", "Positive", sectors="Energy")) == [("XOM", "Energy", "Wait"), ("SMLL", "Energy", "Good")]

    # A ticker outside the tracked list is shown without a verdict.
    untracked = affected_stocks(article("Startup news", "Positive", tickers="ZZZZ"), LOOKUP, by_sector)
    assert untracked == [{"ticker": "ZZZZ", "name": "ZZZZ", "via": None, "tracked": False, "view": None,
                          "reason": "Not in the tracked market list"}]


def test_macro_themes_match_whole_words_only():
    assert find_macro_themes("Warsh's regime change at the Fed")[0] == []        # not "war"
    assert find_macro_themes("Returns rise toward 2027 targets")[0] == []
    assert find_macro_themes("War in Europe lifts oil")[0] == ["war"]
    assert find_macro_themes("New tariffs hit imports")[0] == ["tariff"]         # plural counts


def test_picks_are_positive_news_on_a_rising_trend_best_first():
    results = [
        article("Apple beats", "Positive", tickers="AAPL"),
        article("Apple upgraded", "Positive", tickers="AAPL"),
        article("Microsoft wins deal", "Positive", tickers="MSFT"),
        article("Exxon upgraded", "Positive", tickers="XOM"),        # good news but falling trend: not a pick
        article("Target sued", "Negative", tickers="TGT"),           # bad news: not a pick
        article("Small Energy flat", "Neutral", tickers="SMLL"),     # no clear signal: not a pick
    ]
    suggestions = build_suggestions(results, LOOKUP, MARKET)

    assert [p["ticker"] for p in suggestions["picks"]] == ["AAPL", "MSFT"]   # two positive headlines beat one
    assert all(p["view"] == "Good" for p in suggestions["picks"])
    views = {s["ticker"]: s["view"] for s in suggestions["stocks"]}
    assert views == {"AAPL": "Good", "MSFT": "Good", "XOM": "Wait", "TGT": "Wait", "SMLL": "No clear signal"}
