"""SEC EDGAR, Benzinga and Massive clients, against canned responses shaped
like each provider's documented payloads (no network, no keys)."""
from datetime import date

from fundamentals import sec_edgar
from momentum import market_data
from momentum.momentum_engine import BENCHMARK_TICKER
from news_sentiment import news_sentiment_scraper as scraper


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def fact(start, end, val, filed, form="10-K"):
    row = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        row["start"] = start
    return row


# --- SEC EDGAR ---------------------------------------------------------------

def test_edgar_picks_newest_tag_latest_filing_and_period_length():
    facts = {"us-gaap": {
        # Old tag the company stopped using in 2018: must lose to the newer tag.
        "Revenues": {"units": {"USD": [fact("2017-10-01", "2018-09-29", 265, "2018-11-05")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            fact("2023-10-01", "2024-09-28", 391, "2024-11-01"),
            fact("2023-10-01", "2024-09-28", 390, "2025-10-31"),        # restated later: wins
            fact("2024-09-29", "2025-09-27", 416, "2025-10-31"),
            fact("2026-03-29", "2026-06-27", 109, "2026-07-31", "10-Q"),  # a quarter
            fact("2025-09-28", "2026-06-27", 320, "2026-07-31", "10-Q"),  # 9-month YTD: neither
        ]}},
        "LongTermDebt": {"units": {"USD": [fact(None, "2014-06-30", 999, "2014-08-04")]}},
    }}
    parsed = sec_edgar.parse_fundamentals(facts, today=date(2026, 9, 25))

    assert [(p["period_end"], p["value"]) for p in parsed["annual"]["revenue"]] == [("2025-09-27", 416), ("2024-09-28", 390)]
    assert [p["value"] for p in parsed["quarterly"]["revenue"]] == [109]
    assert parsed["latest"]["long_term_debt"] is None  # 2014 balance is stale, not "latest"
    assert parsed["annual"]["net_income"] == []


def test_edgar_ifrs_filer_prefers_usd():
    facts = {"ifrs-full": {"Revenue": {"units": {
        "TWD": [fact("2024-01-01", "2024-12-31", 2_894_000, "2025-04-17", "20-F")],
        "USD": [fact("2024-01-01", "2024-12-31", 88_268, "2025-04-17", "20-F")],
    }}}}
    revenue = sec_edgar.parse_fundamentals(facts)["annual"]["revenue"]
    assert revenue[0]["value"] == 88_268 and revenue[0]["unit"] == "USD"


def test_edgar_filings_link_to_original_documents():
    submissions = {"filings": {"recent": {
        "form": ["4", "10-Q", "8-K"],
        "accessionNumber": ["0001-26-1", "0000320193-26-000020", "0000320193-26-000019"],
        "filingDate": ["2026-09-24", "2026-07-31", "2026-07-30"],
        "reportDate": ["", "2026-06-27", "2026-07-30"],
        "primaryDocument": ["form4.xml", "aapl-20260627.htm", "8k.htm"],
        "primaryDocDescription": ["", "10-Q", ""],
    }}}
    filings = sec_edgar.parse_filings(320193, submissions)
    assert [f["form"] for f in filings] == ["10-Q", "8-K"]  # Form 4 (insider trade) skipped
    assert filings[0]["url"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm"


# --- Benzinga -------------------------------------------------------------------

BENZINGA_ITEM = {
    "id": 36444586,
    "created": "Mon, 01 Jan 2024 13:35:14 -0400",
    "updated": "Mon, 01 Jan 2024 13:35:15 -0400",
    "title": "Apple &amp; partners rally on <b>record</b> quarter",
    "teaser": "",
    "url": "https://www.benzinga.com/news/24/01/36444586/apple",
    "stocks": [{"name": "AAPL", "exchange": "NASDAQ"}, {"name": "tsm"}],
}


def test_benzinga_articles_use_provider_tickers(monkeypatch):
    calls = []
    monkeypatch.setattr(scraper.requests, "get", lambda url, **kw: calls.append((url, kw)) or FakeResponse([BENZINGA_ITEM]))

    articles = scraper.fetch_benzinga_articles("key123")

    assert calls[0][1]["params"]["token"] == "key123"
    article = articles[0]
    assert article["title"] == "Apple & partners rally on record quarter"
    assert article["benzinga_tickers"] == ["AAPL", "TSM"]
    # Tagged tickers are used as-is, even though the text never says "Taiwan Semiconductor".
    matched = scraper.match_tickers(articles, [("Apple", "AAPL")])
    assert matched[0]["tickers"] == "AAPL, TSM"
    assert matched[0]["match_type"] == "company"


def test_benzinga_edit_is_reclassified():
    original = {"benzinga_id": 1, "updated": "Mon, 01 Jan 2024 13:35:15 -0400"}
    edited = {**original, "updated": "Mon, 01 Jan 2024 15:00:00 -0400"}
    assert scraper._classification_key(original) != scraper._classification_key(edited)


def test_sentiment_falls_back_to_rss_when_benzinga_fails(monkeypatch):
    monkeypatch.setenv("BENZINGA_API_KEY", "bad")
    monkeypatch.setattr(scraper, "fetch_benzinga_articles", lambda key: (_ for _ in ()).throw(RuntimeError("401")))
    rss = [{"source": "CNBC", "title": "Apple shares rise", "summary": "", "link": "https://x/1", "published": ""}]
    monkeypatch.setattr(scraper, "fetch_articles", lambda: rss)
    monkeypatch.setattr(scraper, "save_history", lambda results: None)
    monkeypatch.setattr(scraper, "SENTIMENT_BACKEND", "claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = scraper.collect_sentiment_results()

    assert result["fallback"] is False
    assert result["results"][0]["source"] == "CNBC"


# --- Massive ------------------------------------------------------------------

def test_massive_quotes_from_full_market_snapshot(monkeypatch):
    snapshot = {"tickers": [
        {"ticker": "AAPL", "todaysChangePerc": 1.25, "updated": 1605192894630916600,
         "lastTrade": {"p": 120.5}, "day": {"c": 120.0, "v": 5000}},
        {"ticker": "BRK.B", "min": {"c": 410.0}, "day": {"v": 10}},
        {"ticker": "NOPRICE"},
    ]}
    monkeypatch.setattr(market_data.requests, "get", lambda url, **kw: FakeResponse(snapshot))

    quotes = market_data.fetch_massive_quotes("key")

    assert quotes["AAPL"]["current_price"] == 120.5
    assert quotes["AAPL"]["day_change_pct"] == 1.25
    assert quotes["AAPL"]["quote_time"].startswith("2020-11-12")
    assert quotes["BRK-B"]["current_price"] == 410.0
    assert "NOPRICE" not in quotes


def test_massive_history_steps_back_over_holidays_and_uses_spy_benchmark(monkeypatch):
    by_date = {
        "2026-09-24": [{"T": "AAPL", "c": 150.0}, {"T": "SPY", "c": 660.0}],
        # ~130 calendar days earlier falls on a weekend; Friday has data.
        "2026-05-15": [{"T": "AAPL", "c": 100.0}, {"T": "SPY", "c": 600.0}],
    }
    requested = []

    def fake_get(url, **kw):
        day = url.rsplit("/", 1)[1]
        requested.append(day)
        assert kw["headers"]["Authorization"] == "Bearer key"
        return FakeResponse({"results": by_date.get(day, [])})

    monkeypatch.setattr(market_data.requests, "get", fake_get)

    prices = market_data.fetch_lookback_prices_massive(["AAPL", "MISSING", BENCHMARK_TICKER], 90, "key", today=date(2026, 9, 25))

    assert prices["AAPL"] == {"start": 100.0, "last": 150.0}
    assert prices[BENCHMARK_TICKER] == {"start": 600.0, "last": 660.0}
    assert "MISSING" not in prices
    assert requested[:2] == ["2026-09-25", "2026-09-24"]  # today had no bars yet


def test_massive_quote_failure_is_not_reported_as_stale(monkeypatch, tmp_path):
    rows = [{"ticker": "AAPL", "current_price": 100.0}]
    monkeypatch.setenv("MASSIVE_API_KEY", "key")
    monkeypatch.setattr(market_data, "fetch_universe", lambda limit: list(rows))
    monkeypatch.setattr(market_data, "fetch_massive_quotes", lambda key: (_ for _ in ()).throw(RuntimeError("403")))
    data = market_data.MarketData(cache_path=tmp_path / "cache.json", universe_size=10)

    data.refresh_quotes()

    assert data.last_error is None
    assert data.status()["price_source"] == "Nasdaq screener (~15 min delayed); Massive unavailable"
    assert data._universe[0]["current_price"] == 100.0
