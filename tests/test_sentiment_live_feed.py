import news_sentiment.news_sentiment_scraper as scraper


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
