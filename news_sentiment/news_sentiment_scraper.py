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
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None  # only required when SENTIMENT_BACKEND="claude"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RSS_FEEDS = {
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "CNBC Markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "MarketWatch Top Stories": "https://www.marketwatch.com/rss/topstories",
    "Investing.com Stock News": "https://www.investing.com/rss/news_25.rss",
}

TICKER_CSV = Path(__file__).parent / "company_tickers.csv"
OUTPUT_CSV = Path(__file__).parent / "news_sentiment_output.csv"

# Macro/geopolitical themes that move whole sectors even when no specific
# company is named in the headline — e.g. "Oil prices surge amid Russia-
# Ukraine tensions" never says "Exxon," but every Energy stock is affected.
# This catches what company-name matching alone would miss.
MACRO_THEMES = {
    "war": ["Energy", "Defense", "Financials"],
    "invasion": ["Energy", "Defense"],
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
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 10  # articles per API call (also used as the local model's batch size)


# ---------------------------------------------------------------------------
# Company / ticker matching
# ---------------------------------------------------------------------------

def load_company_map():
    mapping = []
    with open(TICKER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mapping.append((row["company"].strip(), row["ticker"].strip()))
    # Longest names first so "JPMorgan Chase" is tried before "JPMorgan"
    mapping.sort(key=lambda x: -len(x[0]))
    return mapping


def find_tickers(text, company_map):
    found = {}
    lower_text = text.lower()
    for name, ticker in company_map:
        pattern = r"\b" + re.escape(name.lower()) + r"\b"
        if re.search(pattern, lower_text):
            found[ticker] = name
    return found


def find_macro_themes(text):
    """Detects broad macro/geopolitical themes and the sectors they affect,
    even when no specific company is named."""
    lower_text = text.lower()
    matched_themes = []
    affected_sectors = set()
    for theme, sectors in MACRO_THEMES.items():
        if theme in lower_text:
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
effect: an article that beats estimates but cuts guidance is mixed/Neutral \
or Negative depending on which matters more; a lawsuit against a \
competitor can be Positive for the company in question.

Respond with ONLY a JSON array, one object per article, in the same \
order as given, with this exact shape:
[{"id": 1, "sentiment": "Positive", "confidence": 0.8, "reasoning": "short phrase"}, ...]

No prose, no markdown fences, just the JSON array."""


def classify_batch(client, batch):
    """batch: list of dicts with 'title', 'summary', and either 'tickers' or 'sectors_affected'"""
    lines = []
    for i, a in enumerate(batch, start=1):
        if a.get("tickers"):
            tag = f"Tickers: {a['tickers']}"
        else:
            tag = f"Macro theme affecting sectors: {a.get('sectors_affected', 'Unknown')}"
        lines.append(f"{i}. [{tag}] {a['title']} — {a['summary'][:300]}")
    user_prompt = "\n".join(lines)

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()

    # Defensive parsing in case the model wraps output in a code fence anyway
    raw_text = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"[warn] could not parse LLM response, skipping batch:\n{raw_text}", file=sys.stderr)
        return [{"sentiment": "Unknown", "confidence": 0.0, "reasoning": "parse error"} for _ in batch]

    # Map back by id, defaulting to Unknown if the model dropped an entry
    by_id = {item.get("id"): item for item in parsed if isinstance(item, dict)}
    results = []
    for i in range(1, len(batch) + 1):
        item = by_id.get(i, {"sentiment": "Unknown", "confidence": 0.0, "reasoning": "missing from response"})
        results.append(item)
    return results


def classify_all(client, articles):
    """Classifies a list of matched articles, returns list aligned to input order.
    Routes to Claude or your local trained model based on SENTIMENT_BACKEND."""
    if SENTIMENT_BACKEND == "local":
        return classify_all_local(articles)

    all_results = []
    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start:start + BATCH_SIZE]
        all_results.extend(classify_batch(client, batch))
    return all_results


def classify_all_local(articles):
    """Sentiment via your own trained model (news_sentiment/model/)."""
    sys.path.append(str(Path(__file__).parent / "model"))
    from local_sentiment_model import LocalSentimentClassifier  # local import: only needed for this backend

    model_dir = Path(__file__).parent / "model" / "sentiment_model"
    if not model_dir.exists():
        print(
            f"[error] No trained model found at {model_dir}. "
            "Run news_sentiment/model/train_sentiment_model.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

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

def fetch_articles():
    articles = []
    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] could not fetch {source}: {e}", file=sys.stderr)
            continue
        for entry in feed.entries:
            articles.append({
                "source": source,
                "title": entry.get("title", ""),
                "summary": entry.get("summary", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
            })
    return articles


def match_tickers(articles, company_map):
    matched = []
    for a in articles:
        text = f"{a['title']}. {a['summary']}"
        tickers = find_tickers(text, company_map)
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
    client = None
    if SENTIMENT_BACKEND == "claude":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("[error] Set ANTHROPIC_API_KEY in your environment before running (or set SENTIMENT_BACKEND=local).", file=sys.stderr)
            sys.exit(1)
        client = Anthropic(api_key=api_key)

    company_map = load_company_map()

    articles = fetch_articles()
    matched = match_tickers(articles, company_map)

    if not matched:
        print_report([], {})
        return

    sentiments = classify_all(client, matched)

    results = []
    for article, sentiment in zip(matched, sentiments):
        results.append({**article, **sentiment})

    summary = summarize_by_ticker(results)
    sector_summary = summarize_by_sector(results)
    print_report(results, summary, sector_summary)
    save_csv(results)


if __name__ == "__main__":
    main()
