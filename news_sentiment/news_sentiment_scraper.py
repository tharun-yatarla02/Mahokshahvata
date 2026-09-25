"""
news_sentiment_scraper.py

Pulls live financial news headlines from public RSS feeds, detects which
publicly-traded companies are mentioned (matched against a known ticker
list), and scores each article's sentiment (Positive / Negative / Neutral)
using an LLM (Claude) rather than a keyword-based lexicon.

Setup:
    pip install feedparser anthropic
    export ANTHROPIC_API_KEY="your-key-here"

Run:
    python news_sentiment_scraper.py

Output:
    - Prints a live console report (per-article + per-ticker summary)
    - Writes news_sentiment_output.csv with every matched article

Why an LLM instead of a lexicon (e.g. VADER):
    Financial headlines are full of context a keyword list can't capture —
    "beats estimates but slashes guidance" is not simply positive because
    it contains "beats". An LLM reads the whole sentence and can reason
    about net effect, sarcasm, and mixed signals. Trade-off: it costs
    money per call and is slower than a local lexicon, which is why
    articles are batched into a handful of API calls rather than one
    call per article.

Extend later:
    - Swap RSS_FEEDS for your Marketaux API call for more structured data
    - Run this on a schedule (cron / cloud scheduled job) and write to
      your real database instead of a CSV
    - Cache results per article URL so re-runs don't re-score the same
      article twice
"""

import csv
import email.utils
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from pydantic import BaseModel
from typing import Literal

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None  # only required when SENTIMENT_BACKEND="claude"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RSS_FEEDS = {
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "CNBC Markets": [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    ],
    "MarketWatch Top Stories": "https://www.marketwatch.com/rss/topstories",
    "Investing.com Stock News": "https://www.investing.com/rss/news_25.rss",
}

TICKER_CSV = Path(__file__).parent / "company_tickers.csv"
OUTPUT_CSV = Path(__file__).parent / "news_sentiment_output.csv"
HISTORY_FILE = Path(__file__).parent / "sentiment_history.json"
HISTORY_RETENTION_DAYS = 5

# Macro/geopolitical themes that move whole sectors even when no specific
# company is named in the headline — e.g. "Oil prices surge amid Russia-
# Ukraine tensions" never says "Exxon," but every Energy stock is affected.
# This catches what company-name matching alone would miss.
MACRO_THEMES = {
    "war": ["Energy", "Industrials", "Financials"],   # Industrials includes defense
    "invasion": ["Energy", "Industrials"],
    "sanctions": ["Energy", "Financials"],
    "oil price": ["Energy"],
    "opec": ["Energy"],
    "crude": ["Energy"],
    "interest rate": ["Financials", "Real Estate"],
    "federal reserve": ["Financials"],
    "rate cut": ["Financials", "Real Estate", "Technology"],
    "rate hike": ["Financials", "Real Estate"],
    "inflation": ["Consumer Discretionary", "Financials"],
    "recession": ["Financials", "Consumer Discretionary"],
    "tariff": ["Industrials", "Technology", "Consumer Discretionary"],
    "trade war": ["Industrials", "Technology"],
    "supply chain": ["Industrials", "Technology"],
    "chip shortage": ["Technology"],
    "election": ["Financials", "Healthcare", "Energy"],
}

# Which sentiment backend to use: "claude" (Anthropic API) or "local" (your
# own trained model via model/local_sentiment_model.py). Override with:
#   export SENTIMENT_BACKEND=local
SENTIMENT_BACKEND = os.environ.get("SENTIMENT_BACKEND", "claude")

# Fast, cheap model — plenty accurate for short classification tasks like this.
ANTHROPIC_MODEL = "claude-haiku-4-5"
BATCH_SIZE = 10  # articles per API call (also used as the local model's batch size)


# ---------------------------------------------------------------------------
# Company / ticker matching
# ---------------------------------------------------------------------------

# Company names that are also everyday words in market news ("raises price
# target", "block trade", "shell company", "snap election"). Headlines don't
# get tagged by these names; an explicit ticker mention like "(NYSE: TGT)"
# or "$TGT" still tags them.
AMBIGUOUS_NAMES = {
    "Target", "Block", "Square", "Snap", "Zoom", "Shell", "Arm", "Sea", "Nu",
    "Strategy", "Booking", "Progressive", "Southern", "Marsh", "Match", "Gap",
    "Ball", "Chart", "Carrier", "Sun", "Mobile", "Global", "Capital", "Digital",
    "Energy", "General", "American", "United", "National", "First",
    "International", "Coherent", "Fair", "Tapestry", "Globe", "Delta", "Equity",
    "Realty", "Crown", "Ross", "Dollar", "Discover", "Genuine", "Waters",
    "Graham", "Paramount", "Fox", "News", "Trade", "Service", "Universal",
    "Public", "Prudential", "Principal", "Travelers", "Hartford", "Corning",
    "Southwest", "Alaska", "Frontier", "Spirit", "Rocket", "Lucid", "Toast",
    "Wise", "Ford", "Popular", "Northern", "Nova", "Crane", "Dover", "Flex",
    "Freedom", "Grab", "Trip", "Viking", "Reliance", "Carlisle", "Woodward",
    "Equitable", "Elastic", "Affirm",
}

# "$NVDA", "(NASDAQ: NVDA)", "(NYSE:BRK.B)"
TICKER_MENTION = re.compile(
    r"\$([A-Z]{1,5}(?:[.-][A-Z])?)\b"
    r"|\((?:NYSE|NASDAQ|Nasdaq|NYSE American|NYSEArca|NYSEARCA|AMEX|OTC)\s*:\s*([A-Z]{1,5}(?:[.-][A-Z])?)\)"
)


def _normalize_ticker(ticker):
    return ticker.strip().upper().replace(".", "-")  # BRK.B -> BRK-B, as in the market data


def load_company_map():
    mapping = []
    with open(TICKER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mapping.append((row["company"].strip(), _normalize_ticker(row["ticker"])))
    return mapping


def build_matcher(company_map, known_tickers=None):
    """Compiles company names into one case-sensitive pattern (so "apple the
    fruit" doesn't match Apple), skipping AMBIGUOUS_NAMES. known_tickers, if
    given, limits explicit ticker mentions to real tracked symbols."""
    names = {}
    for name, ticker in company_map:
        if name and name not in AMBIGUOUS_NAMES:
            names.setdefault(name, ticker)  # first listing of a name wins
    # Longest first so "JPMorgan Chase" is tried before "JPMorgan".
    alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pattern = re.compile(rf"(?<![\w&])(?:{alternation})(?![\w&])") if names else None
    return {"pattern": pattern, "names": names, "known_tickers": set(known_tickers) if known_tickers else None}


def find_tickers(text, matcher):
    """{ticker: matched name or symbol} for companies named in the text."""
    if not isinstance(matcher, dict):  # a plain [(name, ticker)] list
        matcher = build_matcher(matcher)
    found = {}
    if matcher["pattern"]:
        for match in matcher["pattern"].finditer(text):
            found.setdefault(matcher["names"][match.group(0)], match.group(0))
    for match in TICKER_MENTION.finditer(text):
        ticker = _normalize_ticker(match.group(1) or match.group(2))
        if matcher["known_tickers"] is None or ticker in matcher["known_tickers"]:
            found.setdefault(ticker, ticker)
    return found


def find_macro_themes(text):
    """Detects broad macro/geopolitical themes and the sectors they affect,
    even when no specific company is named."""
    lower_text = text.lower()
    matched_themes = []
    affected_sectors = set()
    for theme, sectors in MACRO_THEMES.items():
        # Whole words only ("war" must not match "Warsh" or "toward"); a plural counts.
        if re.search(rf"\b{re.escape(theme)}s?\b", lower_text):
            matched_themes.append(theme)
            affected_sectors.update(sectors)
    return matched_themes, sorted(affected_sectors)


# ---------------------------------------------------------------------------
# LLM sentiment classification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a financial news sentiment classifier. For each \
numbered article, decide whether the news is Positive, Negative, or \
Neutral for the mentioned stock(s), from an investor's perspective — not \
whether the news itself is happy or sad in a general sense. Consider net \
effect: an article that beats estimates but cuts guidance is Neutral or \
Negative depending on which matters more; a lawsuit against a competitor \
can be Positive for the company in question.

Return one result per article, using the article's number as its id. Keep \
each reasoning to a short phrase."""


# The response shape, enforced by the API (structured outputs): the reply is
# always valid JSON with exactly these fields, and sentiment can only be one
# of the three labels. Free-text JSON occasionally came back malformed (a
# stray brace), which lost the whole batch.
class HeadlineSentiment(BaseModel):
    id: int
    sentiment: Literal["Positive", "Negative", "Neutral"]
    confidence: float
    reasoning: str


class BatchSentiment(BaseModel):
    results: list[HeadlineSentiment]


def classify_batch(client, batch):
    """batch: list of dicts with 'title', 'summary', and either 'tickers' or 'sectors_affected'.
    Returns one {"sentiment", "confidence", "reasoning"} dict per article, in order;
    "Unknown" where the model returned nothing usable (those aren't cached)."""
    lines = []
    for i, a in enumerate(batch, start=1):
        if a.get("tickers"):
            tag = f"Tickers: {a['tickers']}"
        else:
            tag = f"Macro theme affecting sectors: {a.get('sectors_affected', 'Unknown')}"
        lines.append(f"{i}. [{tag}] {a['title']} — {a['summary'][:300]}")

    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        output_format=BatchSentiment,
    )

    parsed = response.parsed_output
    if parsed is None:  # refusal or cut off at max_tokens: nothing validated
        print(f"[warn] no structured output (stop_reason={response.stop_reason}), skipping batch", file=sys.stderr)
        parsed = BatchSentiment(results=[])

    by_id = {item.id: item for item in parsed.results}
    results = []
    for i in range(1, len(batch) + 1):
        item = by_id.get(i)
        if item is None:
            results.append({"sentiment": "Unknown", "confidence": 0.0, "reasoning": "missing from response"})
        else:
            results.append({
                "sentiment": item.sentiment,
                "confidence": round(min(1.0, max(0.0, item.confidence)), 2),
                "reasoning": item.reasoning,
            })
    return results


def classify_all(client, articles):
    """Classifies a list of matched articles, returns list aligned to input order.
    Routes to Claude or your local trained model based on SENTIMENT_BACKEND."""
    if SENTIMENT_BACKEND == "local":
        return classify_all_local(articles)

    # Only send headlines Claude hasn't already classified.
    uncached = [a for a in articles if _classification_key(a) not in _classification_cache]
    for start in range(0, len(uncached), BATCH_SIZE):
        batch = uncached[start:start + BATCH_SIZE]
        for article, result in zip(batch, classify_batch(client, batch)):
            if result.get("sentiment") != "Unknown":  # don't cache parse errors; retry next time
                _classification_cache[_classification_key(article)] = result
    if len(_classification_cache) > CLASSIFICATION_CACHE_MAX:
        _classification_cache.clear()  # ponytail: crude bound, swap for an LRU if the cache churns
    unknown = {"sentiment": "Unknown", "confidence": 0.0, "reasoning": "classification failed"}
    return [_classification_cache.get(_classification_key(a), unknown) for a in articles]


# Per-headline cache so repeat refreshes only pay for new headlines.
CLASSIFICATION_CACHE_MAX = 5000
_classification_cache = {}


def _classification_key(article):
    # Benzinga edits articles in place; a new `updated` time means re-classify.
    if article.get("benzinga_id"):
        return f"benzinga:{article['benzinga_id']}:{article.get('updated', '')}"
    return article.get("link") or article.get("title", "")


def classify_all_local(articles):
    """Sentiment via your own trained model (news_sentiment/model/)."""
    sys.path.append(str(Path(__file__).parent / "model"))
    from local_sentiment_model import LocalSentimentClassifier  # local import: only needed for this backend

    model_dir = Path(__file__).parent / "model" / "sentiment_model"
    if not model_dir.exists():
        # Raise rather than sys.exit(): this runs inside the web server.
        raise RuntimeError(
            f"No trained model found at {model_dir}. "
            "Run news_sentiment/model/train_sentiment_model.py first."
        )

    classifier = LocalSentimentClassifier(str(model_dir))
    texts = [f"{a['title']}. {a['summary'][:300]}" for a in articles]
    predictions = classifier.classify_batch(texts, batch_size=BATCH_SIZE)
    # Local model has no "reasoning" output like the LLM prompt does — fill a placeholder
    for p in predictions:
        p.setdefault("reasoning", "(local model — no explanation generated)")
    return predictions


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _coerce_feed_urls(feed_entry):
    if isinstance(feed_entry, str):
        return [feed_entry]
    if isinstance(feed_entry, (list, tuple)):
        return [url for url in feed_entry if url]
    return []


def _get_entry_timestamp(entry):
    """Publish time as a timezone-aware UTC datetime, or None. Always aware:
    naive and aware datetimes can't be compared, so one feed without a
    timezone would otherwise break sorting for all of them."""
    raw_value = (entry.get("published") or entry.get("updated") or entry.get("pubDate") or "").strip()
    if not raw_value:
        return None
    parsed = None
    try:
        # RSS dates: "Wed, 23 Sep 2026 10:12:00 GMT" / "... -0400"
        parsed = email.utils.parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clean_text(value):
    """Feed text as plain text: drop HTML tags, decode entities (S&amp;P -> S&P).
    The frontend escapes it again before rendering."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def fetch_articles():
    articles = []
    for source, feed_urls in RSS_FEEDS.items():
        urls = _coerce_feed_urls(feed_urls)
        if not urls:
            continue

        best_entries = []
        best_timestamp = None

        for url in urls:
            try:
                response = requests.get(
                    url,
                    timeout=20,
                    headers={"User-Agent": "Mozilla/5.0"},
                    allow_redirects=True,
                )
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                entries = list(getattr(feed, "entries", []))
            except Exception as e:
                print(f"[warn] could not fetch {source} from {url}: {e}", file=sys.stderr)
                continue

            if not entries:
                continue

            feed_latest = max(
                (_get_entry_timestamp(entry) for entry in entries if _get_entry_timestamp(entry) is not None),
                default=None,
            )
            if feed_latest is None or best_timestamp is not None and feed_latest <= best_timestamp:
                continue

            best_entries = entries
            best_timestamp = feed_latest

        for entry in best_entries:
            title = _clean_text(entry.get("title"))
            summary = _clean_text(entry.get("summary") or entry.get("description"))
            link = (entry.get("link") or entry.get("url") or "").strip()
            published = entry.get("published") or entry.get("updated") or entry.get("pubDate") or ""
            if not title and not summary:
                continue
            timestamp = _get_entry_timestamp(entry)
            articles.append({
                "source": source,
                "title": title,
                "summary": summary,
                "link": link,
                "published": published,
                "published_ts": timestamp.isoformat() if timestamp else "",  # sortable UTC time
            })

    deduped = []
    seen = set()
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    # RSS dates are strings like "Wed, 23 Sep 2026 ..." — sort by the parsed time.
    for article in sorted(articles, key=lambda a: _get_entry_timestamp(a) or oldest, reverse=True):
        key = (article.get("title") or "", article.get("link") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(article)
    return deduped


BENZINGA_NEWS_URL = "https://api.benzinga.com/api/v2/news"
BENZINGA_PAGE_SIZE = 100


def fetch_benzinga_articles(api_key):
    """Company news from Benzinga's Newsfeed API, already tagged with tickers,
    so these skip company-name matching. Dates are RFC 2822, like RSS."""
    response = requests.get(
        BENZINGA_NEWS_URL,
        params={"token": api_key, "pageSize": BENZINGA_PAGE_SIZE},  # newest first by default
        headers={"accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    articles = []
    for item in response.json():
        title = _clean_text(item.get("title"))
        if not title:
            continue
        articles.append({
            "source": "Benzinga",
            "title": title,
            "summary": _clean_text(item.get("teaser")),
            "link": (item.get("url") or "").strip(),
            "published": item.get("created") or "",
            "updated": item.get("updated") or "",
            "benzinga_id": item.get("id"),
            "benzinga_tickers": sorted({(s.get("name") or "").upper() for s in item.get("stocks") or [] if s.get("name")}),
        })
    return articles


def match_tickers(articles, company_map, known_tickers=None):
    matcher = build_matcher(company_map, known_tickers)
    matched = []
    names_by_ticker = {}
    for name, ticker in company_map:
        names_by_ticker.setdefault(ticker, name)
    for a in articles:
        text = f"{a['title']}. {a['summary']}"
        if a.get("benzinga_tickers"):
            tickers = {t: names_by_ticker.get(t, t) for t in a["benzinga_tickers"]}
        else:
            tickers = find_tickers(text, matcher)
        themes, sectors = find_macro_themes(text)

        if tickers:
            matched.append({
                **a,
                "tickers": ", ".join(sorted(tickers.keys())),
                "companies": ", ".join(sorted(set(tickers.values()))),
                "match_type": "company",
                "sectors_affected": "",
                "themes": "",
            })
        elif sectors:
            # No specific company named, but a macro theme affects whole sectors
            matched.append({
                **a,
                "tickers": "",
                "companies": "",
                "match_type": "macro",
                "sectors_affected": ", ".join(sectors),
                "themes": ", ".join(themes),
            })
        # else: no company or macro theme match — skip, not relevant to tracked stocks
    return matched


def summarize_by_ticker(results):
    summary = {}
    for r in results:
        if r["sentiment"] == "Unknown" or not r.get("tickers"):
            continue
        for ticker in r["tickers"].split(", "):
            if not ticker:
                continue
            s = summary.setdefault(ticker, {"count": 0, "pos": 0, "neg": 0, "neu": 0})
            s["count"] += 1
            key = {"Positive": "pos", "Negative": "neg", "Neutral": "neu"}.get(r["sentiment"], "neu")
            s[key] += 1
    return summary


def summarize_by_sector(results):
    """Aggregates macro-theme articles (no specific company named) by affected sector."""
    summary = {}
    for r in results:
        if r["sentiment"] == "Unknown" or not r.get("sectors_affected"):
            continue
        for sector in r["sectors_affected"].split(", "):
            if not sector:
                continue
            s = summary.setdefault(sector, {"count": 0, "pos": 0, "neg": 0, "neu": 0})
            s["count"] += 1
            key = {"Positive": "pos", "Negative": "neg", "Neutral": "neu"}.get(r["sentiment"], "neu")
            s[key] += 1
    return summary


def print_report(results, summary, sector_summary=None):
    sector_summary = sector_summary or {}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*90}\nLIVE STOCK NEWS SENTIMENT (LLM-scored) — {stamp}\n{'='*90}\n")

    if not results:
        print("No articles matched known tickers or macro themes in this run.")
        return

    company_results = [r for r in results if r.get("tickers")]
    macro_results = [r for r in results if r.get("sectors_affected")]

    print(f"{len(company_results)} article(s) mentioned tracked companies directly:\n")
    for r in company_results:
        conf = f"{r.get('confidence', 0):.0%}"
        print(f"[{r['sentiment']:8}] (conf {conf:5})  {r['tickers']:20}  {r['title']}")
        print(f"           why: {r.get('reasoning', '')}")
        print(f"           source: {r['source']}   {r['link']}\n")

    if macro_results:
        print(f"{'-'*90}\nMACRO / GEOPOLITICAL NEWS (no company named, whole sectors affected)\n{'-'*90}\n")
        for r in macro_results:
            conf = f"{r.get('confidence', 0):.0%}"
            print(f"[{r['sentiment']:8}] (conf {conf:5})  sectors: {r['sectors_affected']:30}  {r['title']}")
            print(f"           why: {r.get('reasoning', '')}")
            print(f"           source: {r['source']}   {r['link']}\n")

    print(f"{'-'*90}\nPER-TICKER SUMMARY (sorted by article volume)\n{'-'*90}")
    print(f"{'Ticker':8}{'Articles':10}{'Pos':6}{'Neu':6}{'Neg':6}")
    for ticker, s in sorted(summary.items(), key=lambda x: -x[1]["count"]):
        print(f"{ticker:8}{s['count']:<10}{s['pos']:<6}{s['neu']:<6}{s['neg']:<6}")

    if sector_summary:
        print(f"\n{'-'*90}\nPER-SECTOR MACRO IMPACT (sorted by article volume)\n{'-'*90}")
        print(f"{'Sector':22}{'Articles':10}{'Pos':6}{'Neu':6}{'Neg':6}")
        for sector, s in sorted(sector_summary.items(), key=lambda x: -x[1]["count"]):
            print(f"{sector:22}{s['count']:<10}{s['pos']:<6}{s['neu']:<6}{s['neg']:<6}")


def build_demo_sentiment_results():
    """Fallback data used when the live RSS feed is empty or irrelevant.
    This keeps the app feeling alive and avoids a blank sentiment section during
    quiet news cycles."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        {
            "source": "Demo Feed",
            "title": "AI leaders and cloud infrastructure demand continue to support large-cap tech momentum.",
            "summary": "Analysts remain constructive on AI infrastructure, software monetization, and enterprise renewal trends.",
            "link": "https://example.com/demo/ai-tech-momentum",
            "published": now,
            "match_type": "company",
            "tickers": "MSFT, NVDA",
            "companies": "Microsoft, NVIDIA",
            "sectors_affected": "",
            "themes": "",
            "sentiment": "Positive",
            "confidence": 0.82,
            "reasoning": "Demo fallback: live feed was empty or unrelated.",
        },
        {
            "source": "Demo Feed",
            "title": "Broad market optimism stays elevated as rate expectations remain stable.",
            "summary": "Investors price in resilient growth and a relatively stable macro backdrop.",
            "link": "https://example.com/demo/rates-stability",
            "published": now,
            "match_type": "macro",
            "tickers": "",
            "companies": "",
            "sectors_affected": "Financials, Technology",
            "themes": "rate cut, inflation",
            "sentiment": "Neutral",
            "confidence": 0.7,
            "reasoning": "Demo fallback: macro sentiment remained stable while live news was sparse.",
        },
        {
            "source": "Demo Feed",
            "title": "Energy and industrial names are tracking commodity and supply-chain headlines closely.",
            "summary": "Commodity-sensitive sectors remain in focus as investors monitor energy and global trade conditions.",
            "link": "https://example.com/demo/energy-industrials",
            "published": now,
            "match_type": "macro",
            "tickers": "",
            "companies": "",
            "sectors_affected": "Energy, Industrials",
            "themes": "oil price, supply chain",
            "sentiment": "Positive",
            "confidence": 0.71,
            "reasoning": "Demo fallback: sector-level sentiment placeholder.",
        },
    ]


def collect_sentiment_results(extra_companies=(), known_tickers=None):
    """Return the latest relevant sentiment data from the live RSS feed.

    extra_companies: more (name, ticker) pairs to recognize in headlines on
    top of company_tickers.csv (the API passes the largest tracked stocks).
    known_tickers: tracked symbols that explicit mentions like "$NVDA" may
    match.

    The app should prefer real RSS headlines whenever they match tracked names or
    macro themes. A curated demo dataset is only used as a last-resort fallback
    when the feed is empty, disconnected, or unrelated to tracked securities.
    """
    client = None
    if SENTIMENT_BACKEND == "claude":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            client = Anthropic(api_key=api_key)

    company_map = load_company_map() + list(extra_companies)
    articles = []
    benzinga_key = os.environ.get("BENZINGA_API_KEY")
    if benzinga_key:
        try:
            articles = fetch_benzinga_articles(benzinga_key)
        except Exception as e:  # bad key, plan limits, network
            # Not str(e): requests puts the URL, and so the token, in its errors.
            reason = getattr(getattr(e, "response", None), "status_code", None) or type(e).__name__
            print(f"[warn] Benzinga news unavailable ({reason}), using RSS feeds", file=sys.stderr)
    if not articles:
        articles = fetch_articles()
    matched = match_tickers(articles, company_map, known_tickers)

    if not matched:
        demo_results = build_demo_sentiment_results()
        # History is kept on disk for later analysis but not sent to the
        # browser: no page uses it, and it grows by a full snapshot per refresh.
        save_history(demo_results)
        return {
            "results": demo_results,
            "summary": summarize_by_ticker(demo_results),
            "sector_summary": summarize_by_sector(demo_results),
            "fallback": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    if SENTIMENT_BACKEND == "claude" and client is None:
        # Real headlines are available, but the LLM key is missing. In that case
        # we still return the live matches with a lightweight heuristic sentiment
        # instead of silently replacing them with stale demo data.
        results = heuristic_sentiment(matched, "Heuristic sentiment fallback (no Anthropic API key)")
    else:
        try:
            sentiments = classify_all(client, matched)
            results = [{**article, **sentiment} for article, sentiment in zip(matched, sentiments)]
        except Exception as e:  # API error, rate limit, network, missing local model
            print(f"[warn] sentiment classification failed, using heuristic: {e}", file=sys.stderr)
            results = heuristic_sentiment(matched, "Heuristic sentiment fallback (classifier unavailable)")

    save_history(results)
    return {
        "results": results,
        "summary": summarize_by_ticker(results),
        "sector_summary": summarize_by_sector(results),
        "fallback": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# Whole words with their forms spelled out: matching substrings labelled
# "prosecutors" negative ("cut"), "against" positive ("gain") and "surprise"
# positive ("rise").
POSITIVE_WORDS = {
    "rise", "rises", "rising", "rose", "rally", "rallies", "rallied", "rallying",
    "beat", "beats", "surge", "surges", "surged", "surging", "upgrade", "upgrades", "upgraded",
    "growth", "strong", "stronger", "strongest", "higher", "gain", "gains", "gained", "gaining",
}
NEGATIVE_WORDS = {
    "drop", "drops", "dropped", "dropping", "fall", "falls", "fell", "falling", "fallen",
    "miss", "misses", "missed", "slump", "slumps", "slumped", "cut", "cuts", "cutting",
    "decline", "declines", "declined", "declining", "weak", "weaker", "weakest", "weakness",
    "lower", "loss", "losses",
}


def heuristic_sentiment(articles, reasoning):
    """Keyword-based sentiment for when no classifier is available."""
    results = []
    for article in articles:
        text = f"{article.get('title', '')} {article.get('summary', '')}".strip().lower()
        if not text:
            results.append({**article, "sentiment": "Neutral", "confidence": 0.0, "reasoning": "No headline text available"})
            continue
        words = re.findall(r"[a-z]+", text)
        positive = sum(word in POSITIVE_WORDS for word in words)
        negative = sum(word in NEGATIVE_WORDS for word in words)
        # More matches wins, so "shares fall as early gains fade" isn't Positive.
        sentiment = "Positive" if positive > negative else "Negative" if negative > positive else "Neutral"
        results.append({**article, "sentiment": sentiment, "confidence": 0.65, "reasoning": reasoning})
    return results


def load_history_store():
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data


def prune_history(history):
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    kept = []
    for item in history:
        try:
            fetched_at = item.get("fetched_at")
            if not fetched_at:
                continue
            dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            if dt >= cutoff:
                kept.append(item)
        except Exception:
            continue
    return kept


def save_history(results):
    history = load_history_store()
    entry = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    history.append(entry)
    history = prune_history(history)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    return history


def save_csv(results):
    if not results:
        return
    fieldnames = ["source", "title", "summary", "link", "published", "match_type",
                  "tickers", "companies", "sectors_affected", "themes",
                  "sentiment", "confidence", "reasoning"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\nSaved {len(results)} rows to {OUTPUT_CSV}")


def main():
    data = collect_sentiment_results()
    results = data["results"]
    summary = data["summary"]
    sector_summary = data["sector_summary"]

    if data["fallback"]:
        print("[info] Live news feed was empty or unrelated; using demo sentiment fallback.")

    print_report(results, summary, sector_summary)
    save_csv(results)


if __name__ == "__main__":
    main()
