"""
suggestions.py

Turns classified headlines into suggestions for the Sentiment page, and
builds the company-name list the scraper matches headlines against.

- company_aliases(): names for the largest tracked stocks, cleaned of legal
  suffixes ("Apple Inc." -> "Apple"). The scraper skips ones that are also
  everyday words (AMBIGUOUS_NAMES: "Target", "Shell", ...).
- build_suggestions(): per stock and per sector, what the news says
  (mentions, positive/negative split, latest headline) next to its present
  market condition (price, today's move, 90-day trend).

Both take plain rows from momentum/market_data.py, so they're easy to test.
"""

import re
import statistics

from news_sentiment.news_sentiment_scraper import AMBIGUOUS_NAMES

NAME_SUFFIX = re.compile(
    r"(,|\s)+(Inc|Incorporated|Corp|Corporation|Company|Companies|Co|Holdings?|Group|Ltd|"
    r"Limited|plc|PLC|N\.?V|S\.?A|SE|AG|A/S|LP|L\.P|Trust|Class [A-Z]|Common|Ordinary|"
    r"Shares?|Stock|&|and|\(The\))\b\.?\s*$",
    re.IGNORECASE,
)
# Share-class and listing wording the screener puts after the company name:
# "SAP SE ADS", "Brookfield Corporation Class A Limited Voting Shares",
# "MPLX LP Common Units Representing Limited Partner Interests". Everything
# from the first of these words on is dropped.
LISTING_DETAIL = re.compile(
    r"\s(?:Class [A-Z](?:-\d)?\b|Series [A-Z]\b|Sponsored|Sponosred|Unsponsored|ADS\b|ADR\b|"
    r"American Depositary|American Depository|N\.?Y\.? Registry|New York Registry|Common Units|"
    r"Common Stock|Ordinary Shares|Limited Voting|Sub\. Vot|REIT\b|Public Limited Company|\(each representing).*$"
)
# "(Holding Company)", "(NEW)", "(Delaware)", "(Japan)", "(COPEL)"
PARENTHETICAL = re.compile(r"\s*\([^)]*\)")
# Bonds, notes and preferreds trade under their own tickers but are never
# what a headline means by the company's name.
DEBT_SECURITY = re.compile(r"\b(?:Notes?|Bonds?|Debentures?|Pfd|Preferred|Perpetual|STRATS|ZONES)\b|\d%")
# "Brookfield Renewable Corporation Brookfield Renewable Corporation Class A ..."
REPEATED_NAME = re.compile(r"^(.+?)\s+(?:Corporation|Corp\.?|Inc\.?)\s+\1\b.*$")
ALIAS_UNIVERSE_SIZE = 1000


def clean_company_name(name):
    """'Eli Lilly and Company' -> 'Eli Lilly', 'Amazon.com, Inc.' -> 'Amazon',
    'SAP SE ADS' -> 'SAP'. Empty for debt securities ("... 5.0% Notes")."""
    name = re.sub(r"\s+", " ", name or "").strip()
    if DEBT_SECURITY.search(name):
        return ""
    name = name.removeprefix("The ").removeprefix("D/B/A ")
    name = PARENTHETICAL.sub(" ", name).strip()
    name = REPEATED_NAME.sub(r"\1", name)
    name = LISTING_DETAIL.sub("", name)
    name = re.sub(r"\s(?:S\.\s?A\.\s?(?:B\.\s)?de C\.V|S\.A|p\.l\.c|L\.P|LLC|Inc\.? New|New|NEW)\.?$", "", name)
    previous = None
    while previous != name:
        previous = name
        name = NAME_SUFFIX.sub("", name).strip(" ,.&")
    name = re.sub(r"\.com$", "", name)
    return name.strip()


def company_aliases(rows, limit=ALIAS_UNIVERSE_SIZE):
    """[(name, ticker)] for the `limit` largest stocks, safe to match
    case-sensitively in headlines. Largest company wins a shared name."""
    aliases = {}
    for row in sorted(rows, key=lambda r: r.get("market_cap") or 0, reverse=True)[:limit]:
        name = clean_company_name(row.get("name"))
        words = name.split()
        if not words or name in AMBIGUOUS_NAMES or name in aliases:
            continue
        if len(words) == 1 and len(name) < 4 and not name.isupper():
            continue  # "Nu", "Sea": too short to be distinctive
        if len(name) < 3:
            continue
        aliases[name] = row["ticker"]
    return list(aliases.items())


# -- suggestions ---------------------------------------------------------------

SENTIMENT_KEYS = {"Positive": "positive", "Negative": "negative", "Neutral": "neutral"}
MAX_STOCKS = 12
MAX_PICKS = 10
MAX_SECTORS = 8
LEADER_MIN_MARKET_CAP = 10_000_000_000  # sector leaders are $10B+ companies


def _tone(positive, negative, mentions):
    """News tone from -1 (all negative) to +1 (all positive)."""
    return round((positive - negative) / mentions, 2) if mentions else 0.0


def _tone_label(tone):
    if tone >= 0.25:
        return "Positive"
    if tone <= -0.25:
        return "Negative"
    return "Mixed"


def stock_signal(tone, momentum_pct, day_change_pct):
    """One plain-language line combining the news with the price trend.
    Informational only, not a recommendation."""
    label = _tone_label(tone)
    trend_up = (momentum_pct or 0) >= 0
    if label == "Positive":
        return "Good news on a rising trend" if trend_up else "Good news on a falling trend: watch for a turn"
    if label == "Negative":
        return "Bad news on a rising trend: watch closely" if trend_up else "Bad news on a falling trend: caution"
    if day_change_pct is not None and abs(day_change_pct) >= 3:
        return f"In the news and moving {'up' if day_change_pct > 0 else 'down'} sharply today"
    return "In the news, no clear direction"


def investment_view(sentiment, momentum_pct):
    """Short verdict for one headline and one stock, using the same rule as
    stock_signal (news tone + direction of the 90-day trend), so the badge
    and the explanation always agree. Informational only."""
    trend_up = (momentum_pct or 0) >= 0
    if sentiment == "Positive":
        return "Good" if trend_up else "Wait"
    if sentiment == "Negative":
        return "Wait" if trend_up else "Bad"
    return "No clear signal"


MAX_AFFECTED = 4
SECTOR_EXAMPLES = 2  # largest companies shown per sector for sector-wide news


def affected_stocks(article, lookup, largest_by_sector):
    """The stocks one headline affects, each with a verdict: the tickers it
    names, or for sector-wide news (rates, oil...) the largest companies in
    the affected sectors, marked with the sector they stand in for.
    largest_by_sector: {sector: [rows, largest market cap first]}."""
    tone = {"Positive": 1, "Negative": -1}.get(article.get("sentiment"), 0)

    def entry(ticker, row, via=None):
        if not row:
            return {"ticker": ticker, "name": ticker, "via": via, "tracked": False,
                    "view": None, "reason": "Not in the tracked market list"}
        return {
            "ticker": ticker, "name": row.get("name") or ticker, "via": via, "tracked": True,
            "current_price": row.get("current_price"), "day_change_pct": row.get("day_change_pct"),
            "momentum_pct": row.get("momentum_pct"),
            "view": investment_view(article.get("sentiment"), row.get("momentum_pct")),
            "reason": stock_signal(tone, row.get("momentum_pct"), row.get("day_change_pct")),
        }

    named = [t.strip() for t in (article.get("tickers") or "").split(",") if t.strip()]
    if named:
        return [entry(t, lookup(t)) for t in named[:MAX_AFFECTED]]
    affected = []
    for sector in (s.strip() for s in (article.get("sectors_affected") or "").split(",")):
        for row in largest_by_sector.get(sector, [])[:SECTOR_EXAMPLES]:
            affected.append(entry(row["ticker"], row, via=sector))
    return affected[:MAX_AFFECTED]


def largest_by_sector(market_rows):
    """{sector: rows sorted by market cap}: sector-wide news is shown against
    the biggest names (not the best performers, which would bias verdicts)."""
    groups = {}
    for row in sorted(market_rows, key=lambda r: r.get("market_cap") or 0, reverse=True):
        groups.setdefault(row.get("sector"), []).append(row)
    return groups


def _new_bucket():
    return {"mentions": 0, "positive": 0, "negative": 0, "neutral": 0, "headlines": []}


def _add(bucket, article):
    bucket["mentions"] += 1
    bucket[SENTIMENT_KEYS.get(article.get("sentiment"), "neutral")] += 1
    bucket["headlines"].append(article)


def _latest(headlines):
    latest = max(headlines, key=lambda a: a.get("published_ts") or "")
    return {k: latest.get(k) for k in ("title", "link", "source", "published", "sentiment")}


def _sector_condition(sector_rows):
    """Present condition of a sector from the tracked stocks in it."""
    if not sector_rows:
        return None
    day_moves = [r["day_change_pct"] for r in sector_rows if r.get("day_change_pct") is not None]
    leaders = sorted(
        (r for r in sector_rows if (r.get("market_cap") or 0) >= LEADER_MIN_MARKET_CAP),
        key=lambda r: r["momentum_pct"], reverse=True,
    )[:3]
    return {
        "stocks_tracked": len(sector_rows),
        "median_day_change_pct": round(statistics.median(day_moves), 2) if day_moves else None,
        "advancing_pct": round(100 * sum(m > 0 for m in day_moves) / len(day_moves), 1) if day_moves else None,
        "median_momentum_pct": round(statistics.median(r["momentum_pct"] for r in sector_rows), 2),
        "leaders": [
            {k: r.get(k) for k in ("ticker", "name", "current_price", "day_change_pct", "momentum_pct")}
            for r in leaders
        ],
    }


def build_suggestions(results, lookup, market_rows):
    """results: classified articles from the scraper. lookup(ticker) -> the
    tracked market row or None. market_rows: every tracked row (for sector
    conditions). Returns {"picks": [...], "stocks": [...], "sectors": [...],
    "overview": {...}}; picks are the stocks with positive news on a rising
    trend (verdict "Good"), best first."""
    stocks, sectors = {}, {}
    for article in results:
        if article.get("sentiment") == "Unknown":
            continue
        tickers = [t.strip() for t in (article.get("tickers") or "").split(",") if t.strip()]
        article_sectors = set()
        for ticker in tickers:
            _add(stocks.setdefault(ticker, _new_bucket()), article)
            row = lookup(ticker)
            if row and row.get("sector") not in (None, "", "Unknown"):
                article_sectors.add(row["sector"])
        article_sectors.update(s.strip() for s in (article.get("sectors_affected") or "").split(",") if s.strip())
        for sector in article_sectors:
            _add(sectors.setdefault(sector, _new_bucket()), article)

    stock_items = []
    for ticker, b in stocks.items():
        row = lookup(ticker) or {}
        tone = _tone(b["positive"], b["negative"], b["mentions"])
        stock_items.append({
            "ticker": ticker,
            "name": row.get("name") or ticker,
            "sector": row.get("sector"),
            "mentions": b["mentions"], "positive": b["positive"], "negative": b["negative"], "neutral": b["neutral"],
            "tone": tone,
            "tone_label": _tone_label(tone),
            "latest_headline": _latest(b["headlines"]),
            "condition": {k: row.get(k) for k in ("current_price", "day_change_pct", "momentum_pct", "relative_strength")} if row else None,
            "signal": stock_signal(tone, row.get("momentum_pct"), row.get("day_change_pct")) if row else "In the news (not in the tracked market list)",
        })
    # Most-covered first; among equals, the clearest (most one-sided) news.
    stock_items.sort(key=lambda s: (s["mentions"], abs(s["tone"])), reverse=True)
    for item in stock_items:
        item["view"] = investment_view(item["tone_label"], item["condition"]["momentum_pct"]) if item["condition"] else None
    # "Good to invest in right now": positive news on a rising trend, the most
    # net-positive coverage first, then the strongest against the S&P 500.
    picks = sorted(
        (s for s in stock_items if s["view"] == "Good"),
        key=lambda s: (s["positive"] - s["negative"], s["tone"], s["condition"].get("relative_strength") or 0),
        reverse=True,
    )[:MAX_PICKS]

    rows_by_sector = {}
    for row in market_rows:
        rows_by_sector.setdefault(row.get("sector"), []).append(row)
    sector_items = []
    for sector, b in sectors.items():
        tone = _tone(b["positive"], b["negative"], b["mentions"])
        sector_items.append({
            "sector": sector,
            "mentions": b["mentions"], "positive": b["positive"], "negative": b["negative"], "neutral": b["neutral"],
            "tone": tone,
            "tone_label": _tone_label(tone),
            "latest_headline": _latest(b["headlines"]),
            "condition": _sector_condition(rows_by_sector.get(sector, [])),
        })
    sector_items.sort(key=lambda s: (s["mentions"], abs(s["tone"])), reverse=True)

    counted = [a for a in results if a.get("sentiment") != "Unknown"]
    return {
        "picks": picks,
        "stocks": stock_items[:MAX_STOCKS],
        "sectors": sector_items[:MAX_SECTORS],
        "overview": {
            "headlines": len(counted),
            "positive": sum(a.get("sentiment") == "Positive" for a in counted),
            "negative": sum(a.get("sentiment") == "Negative" for a in counted),
            "neutral": sum(a.get("sentiment") not in ("Positive", "Negative") for a in counted),
            "stocks_mentioned": len(stock_items),
            "sectors_mentioned": len(sector_items),
        },
    }
