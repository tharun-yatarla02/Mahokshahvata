import pytest

import news_sentiment.news_sentiment_scraper as scraper


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Keep tests from writing to the real sentiment_history.json."""
    monkeypatch.setattr(scraper, "HISTORY_FILE", tmp_path / "sentiment_history.json")


def test_collect_sentiment_results_uses_live_feed_when_matches_exist(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(scraper, "fetch_articles", lambda: [{
        "source": "Yahoo Finance",
        "title": "Apple stock rallies as iPhone demand stays strong",
        "summary": "AAPL shares climb after management reports strong demand and pricing power.",
        "link": "https://example.com/aapl-live",
        "published": "2026-09-23T12:00:00Z",
    }])
    monkeypatch.setattr(scraper, "load_company_map", lambda: [("Apple", "AAPL")])
    monkeypatch.setattr(scraper, "classify_all", lambda client, articles: [{
        "sentiment": "Positive",
        "confidence": 0.91,
        "reasoning": "Fresh headline matched AAPL",
    } for _ in articles])

    data = scraper.collect_sentiment_results()

    assert data["fallback"] is False
    assert data["results"][0]["source"] == "Yahoo Finance"
    assert data["results"][0]["tickers"] == "AAPL"


def test_fetch_articles_uses_http_response_xml(monkeypatch):
    xml = b'''<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>AI chip demand lifts semiconductor outlook</title>
          <description>Chipmakers gain as pricing power stays strong.</description>
          <link>https://example.com/live-chip-news</link>
          <pubDate>Wed, 23 Sep 2026 15:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>'''

    class FakeResponse:
        content = xml
        text = xml.decode("utf-8")

        def raise_for_status(self):
            return None

    monkeypatch.setattr(scraper.requests, "get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(scraper, "RSS_FEEDS", {"Live Feed": "https://example.com/rss"})

    articles = scraper.fetch_articles()

    assert len(articles) == 1
    assert articles[0]["title"] == "AI chip demand lifts semiconductor outlook"
    assert articles[0]["link"] == "https://example.com/live-chip-news"


def test_fetch_articles_tries_alternative_feed_urls_when_primary_is_stale(monkeypatch):
    stale_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>Old headline from September 20</title>
          <description>Stale entry that should be ignored.</description>
          <link>https://example.com/old</link>
          <pubDate>Sun, 20 Sep 2026 15:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>'''

    fresh_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>Fresh headline from September 23</title>
          <description>Live market update from the newer feed.</description>
          <link>https://example.com/new</link>
          <pubDate>Wed, 23 Sep 2026 15:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>'''

    class FakeResponse:
        def __init__(self, payload):
            self.content = payload
            self.text = payload.decode("utf-8")

        def raise_for_status(self):
            return None

    def fake_get(url, *args, **kwargs):
        if url == "https://example.com/stale-cnbc":
            return FakeResponse(stale_xml)
        if url == "https://example.com/fresh-cnbc":
            return FakeResponse(fresh_xml)
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(scraper.requests, "get", fake_get)
    monkeypatch.setattr(scraper, "RSS_FEEDS", {"CNBC Markets": [
        "https://example.com/stale-cnbc",
        "https://example.com/fresh-cnbc",
    ]})

    articles = scraper.fetch_articles()

    assert len(articles) == 1
    assert articles[0]["link"] == "https://example.com/new"
    assert "September 23" in articles[0]["title"]


def test_classify_all_only_sends_new_headlines(monkeypatch):
    monkeypatch.setattr(scraper, "SENTIMENT_BACKEND", "claude")
    monkeypatch.setattr(scraper, "_classification_cache", {})
    sent = []

    def fake_batch(client, batch):
        sent.extend(a["link"] for a in batch)
        return [{"sentiment": "Positive", "confidence": 0.9, "reasoning": "x"} for _ in batch]

    monkeypatch.setattr(scraper, "classify_batch", fake_batch)
    a = {"title": "A", "summary": "", "link": "https://example.com/a"}
    b = {"title": "B", "summary": "", "link": "https://example.com/b"}

    scraper.classify_all(None, [a])
    results = scraper.classify_all(None, [a, b])

    assert sent == ["https://example.com/a", "https://example.com/b"]
    assert [r["sentiment"] for r in results] == ["Positive", "Positive"]


def test_feed_text_is_plain_text():
    assert scraper._clean_text("S&amp;P 500 hits record") == "S&P 500 hits record"
    assert scraper._clean_text("<p>Stocks <b>rally</b></p>\n after CPI") == "Stocks rally after CPI"
    assert scraper._clean_text(None) == ""


APPLE_ARTICLE = {
    "source": "Yahoo Finance",
    "title": "Apple stock rallies as iPhone demand stays strong",
    "summary": "AAPL shares climb.",
    "link": "https://example.com/aapl",
    "published": "2026-09-23T12:00:00Z",
}


def test_classifier_failure_falls_back_to_heuristic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(scraper, "fetch_articles", lambda: [APPLE_ARTICLE])
    monkeypatch.setattr(scraper, "load_company_map", lambda: [("Apple", "AAPL")])

    def broken_classifier(client, articles):
        raise RuntimeError("529 overloaded")
    monkeypatch.setattr(scraper, "classify_all", broken_classifier)

    data = scraper.collect_sentiment_results()

    assert data["results"][0]["sentiment"] == "Positive"   # "rallies" / "strong"
    assert "fallback" in data["results"][0]["reasoning"].lower()
    assert "history" not in data                             # saved to disk, not sent to the browser
    assert scraper.HISTORY_FILE.exists()


def test_articles_are_sorted_by_publish_time_not_date_text(monkeypatch):
    feed = """<?xml version="1.0"?><rss><channel>
      <item><title>Older (Wednesday)</title><link>https://e.com/1</link><pubDate>Wed, 23 Sep 2026 09:00:00 GMT</pubDate></item>
      <item><title>Newer (Thursday)</title><link>https://e.com/2</link><pubDate>Thu, 24 Sep 2026 09:00:00 GMT</pubDate></item>
    </channel></rss>"""

    class Response:
        content = feed.encode()

        def raise_for_status(self):
            pass

    monkeypatch.setattr(scraper, "RSS_FEEDS", {"Test": "https://e.com/rss"})
    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: Response())

    titles = [a["title"] for a in scraper.fetch_articles()]

    assert titles == ["Newer (Thursday)", "Older (Wednesday)"]   # "W" > "T" alphabetically


@pytest.mark.parametrize("raw, expected", [
    ("Wed, 23 Sep 2026 10:00:00 GMT", "2026-09-23T10:00:00+00:00"),
    ("Wed, 23 Sep 2026 10:00:00 -0400", "2026-09-23T14:00:00+00:00"),
    ("2026-09-23T10:00:00Z", "2026-09-23T10:00:00+00:00"),
    ("2026-09-23T10:00:00", "2026-09-23T10:00:00+00:00"),      # no timezone: assume UTC
    ("2026-09-23 10:00:00", "2026-09-23T10:00:00+00:00"),
    ("not a date", None),
])
def test_entry_timestamps_are_always_timezone_aware(raw, expected):
    parsed = scraper._get_entry_timestamp({"published": raw})

    assert (parsed.isoformat() if parsed else None) == expected
