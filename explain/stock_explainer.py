"""
stock_explainer.py

"Explain this stock": a plain-English read of one ticker for the Market
page, built only from data the app already has.

- build_facts(): the numbers and headlines behind the explanation: price
  and today's move, the 90-day trend against the S&P 500 and the stock's
  sector, news tone (headlines naming it, plus sector-wide macro news) and
  SEC fundamentals. Plain dicts in, plain dict out, so it's easy to test.
- explain_with_rules(): the explanation written from fixed rules. Always
  available, no API key needed.
- explain_with_claude(): the same sections written by Claude from the
  facts (and only the facts). Used when ANTHROPIC_API_KEY is set.

Both return the StockExplanation shape. Informational only: neither gives
buy/sell advice.
"""

import json
import statistics

from pydantic import BaseModel

from news_sentiment.suggestions import _tone, _tone_label, stock_signal

ANTHROPIC_MODEL = "claude-opus-5"
MAX_COMPANY_HEADLINES = 6
MAX_SECTOR_HEADLINES = 3
SMALL_CAP = 2_000_000_000
BIG_DAY_MOVE_PCT = 5
DISCLAIMER = "This explains the data the app has. It is not investment advice."


class StockExplanation(BaseModel):
    summary: str             # two or three sentences: what is going on
    why_moving: list[str]    # what the news and the sector say about the move
    trend: list[str]         # 90-day trend, vs the S&P 500 and the sector
    financials: list[str]    # revenue, profit, cash flow, debt
    risks: list[str]         # things to watch
    bottom_line: str         # one sentence, informational, no advice


# -- facts ---------------------------------------------------------------------

def _tickers(article):
    return [t.strip() for t in (article.get("tickers") or "").split(",") if t.strip()]


def _headline(article):
    return {k: article.get(k) for k in ("title", "source", "published", "sentiment", "reasoning", "link")}


def _newest_first(articles):
    return sorted(articles, key=lambda a: a.get("published_ts") or a.get("published") or "", reverse=True)


def _yoy(series):
    """Latest value, its period and the change on the year before."""
    if not series:
        return None
    latest = series[0]
    point = {"value": latest.get("value"), "unit": latest.get("unit"), "period_end": latest.get("period_end")}
    if len(series) > 1 and series[1].get("value"):
        previous = series[1]["value"]
        point["change_pct"] = round((latest["value"] - previous) / abs(previous) * 100, 1)
    return point


def _fundamental_facts(fundamentals):
    if not fundamentals:
        return None
    annual = fundamentals.get("annual") or {}
    facts = {name: _yoy(annual.get(name)) for name in ("revenue", "net_income", "eps_diluted", "operating_cash_flow")}
    debt = (fundamentals.get("latest") or {}).get("long_term_debt")
    if debt:
        facts["long_term_debt"] = {"value": debt.get("value"), "unit": debt.get("unit"), "period_end": debt.get("period_end")}
    facts = {k: v for k, v in facts.items() if v}
    return facts or None


def build_facts(row, market_rows, news_results, fundamentals=None, keyword_tone=False):
    """Everything the explanation may say about one stock.

    row: the stock's market row. market_rows: every tracked row (for the
    sector comparison). news_results: classified headlines from the
    sentiment feed. fundamentals: get_fundamentals() output, or None.
    keyword_tone: True when headline tone came from the keyword fallback."""
    ticker, sector = row["ticker"], row.get("sector")
    facts = {
        "ticker": ticker,
        "name": row.get("name") or ticker,
        "sector": sector,
        "industry": row.get("industry"),
        "market_cap": row.get("market_cap"),
        "current_price": row.get("current_price"),
        "day_change_pct": row.get("day_change_pct"),
        "momentum_90d_pct": row.get("momentum_pct"),
        "vs_sp500_pts": row.get("relative_strength"),
    }

    peers = [r for r in market_rows if r.get("sector") == sector and r.get("momentum_pct") is not None]
    if sector not in (None, "", "Unknown") and len(peers) > 1:
        ranked = sorted(peers, key=lambda r: r["momentum_pct"], reverse=True)
        rank = next((i for i, r in enumerate(ranked, start=1) if r["ticker"] == ticker), None)
        day_moves = [r["day_change_pct"] for r in peers if r.get("day_change_pct") is not None]
        facts["sector_comparison"] = {
            "stocks_in_sector": len(peers),
            "rank_by_90d_trend": rank,
            "sector_median_90d_pct": round(statistics.median(r["momentum_pct"] for r in peers), 2),
            "sector_median_day_pct": round(statistics.median(day_moves), 2) if day_moves else None,
        }

    known = [a for a in news_results if a.get("sentiment") != "Unknown"]
    company = _newest_first([a for a in known if ticker in _tickers(a)])
    sector_news = _newest_first([
        a for a in known if not _tickers(a)
        and sector in [s.strip() for s in (a.get("sectors_affected") or "").split(",")]
    ])
    positive = sum(a.get("sentiment") == "Positive" for a in company)
    negative = sum(a.get("sentiment") == "Negative" for a in company)
    tone = _tone(positive, negative, len(company))
    facts["news"] = {
        "company_mentions": len(company),
        "positive": positive,
        "negative": negative,
        "tone": tone,
        "tone_label": _tone_label(tone) if company else None,
        "tone_is_keyword_estimate": keyword_tone,
        "company_headlines": [_headline(a) for a in company[:MAX_COMPANY_HEADLINES]],
        "sector_headlines": [
            {**_headline(a), "themes": a.get("themes")} for a in sector_news[:MAX_SECTOR_HEADLINES]
        ],
    }
    facts["fundamentals"] = _fundamental_facts(fundamentals)
    facts["signal"] = stock_signal(tone, row.get("momentum_pct"), row.get("day_change_pct"))
    return facts


# -- rule-based explanation ----------------------------------------------------

def _pct(value, digits=1):
    return f"{'+' if value >= 0 else ''}{value:.{digits}f}%"


def _money(value):
    sign, value = ("-" if value < 0 else ""), abs(value)
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if value >= size:
            return f"{sign}${value / size:.1f}{suffix}"
    return f"{sign}${value:,.0f}"


def _reported(point):
    if point.get("unit") == "USD/shares":
        return f"${point['value']:.2f}"
    return _money(point["value"])


def _with_change(label, point):
    text = f"{label}: {_reported(point)} (year ending {point['period_end']})"
    if "change_pct" in point:
        text += f", {_pct(point['change_pct'])} on the year before"
    return text


def explain_with_rules(facts):
    name, ticker = facts["name"], facts["ticker"]
    day, trend = facts.get("day_change_pct"), facts.get("momentum_90d_pct")
    news, sector, comparison = facts["news"], facts.get("sector"), facts.get("sector_comparison")

    summary = f"{name} ({ticker}) trades at ${facts['current_price']:.2f}"
    if day is not None:
        summary += f", {'up' if day >= 0 else 'down'} {abs(day):.2f}% today"
    if trend is not None:
        summary += f" and {'up' if trend >= 0 else 'down'} {abs(trend):.1f}% over about 90 days"
    summary += "."
    if news["company_mentions"]:
        summary += (f" It appears in {news['company_mentions']} recent headline"
                    f"{'s' if news['company_mentions'] != 1 else ''}, and their tone is {news['tone_label'].lower()}.")
    else:
        summary += " No recent headlines name it."

    why = []
    if news["company_mentions"]:
        why.append(f"{news['positive']} positive and {news['negative']} negative of {news['company_mentions']} "
                   f"headlines that name {ticker}.")
        latest = news["company_headlines"][0]
        why.append(f"Latest: \"{latest['title']}\" ({latest.get('source') or 'news'}, {latest['sentiment'].lower()}).")
    for article in news["sector_headlines"][:2]:
        theme = f" about {article['themes']}" if article.get("themes") else ""
        why.append(f"Sector-wide news{theme} affects {sector}: \"{article['title']}\" ({article['sentiment'].lower()}).")
    if comparison and comparison.get("sector_median_day_pct") is not None and day is not None:
        median = comparison["sector_median_day_pct"]
        same_way = (day >= 0) == (median >= 0)
        why.append(f"{sector} stocks moved a median {_pct(median, 2)} today, so {ticker} is moving "
                   f"{'with' if same_way else 'against'} its sector.")
    if not why:
        why.append("No headlines or sector data explain today's move.")

    trend_points = []
    if trend is not None:
        trend_points.append(f"90-day change: {_pct(trend)}.")
    if facts.get("vs_sp500_pts") is not None:
        rs = facts["vs_sp500_pts"]
        trend_points.append(f"{'Ahead of' if rs >= 0 else 'Behind'} the S&P 500 by {abs(rs):.1f} percentage points over the same period.")
    if comparison and comparison.get("rank_by_90d_trend"):
        rank, total = comparison["rank_by_90d_trend"], comparison["stocks_in_sector"]
        trend_points.append(f"#{rank} of {total} tracked {sector} stocks by 90-day trend "
                            f"(sector median {_pct(comparison['sector_median_90d_pct'])}).")
    if not trend_points:
        trend_points.append("No trend data yet: the price history is still loading.")

    fundamentals = facts.get("fundamentals") or {}
    financials = []
    labels = {"revenue": "Revenue", "net_income": "Net income", "eps_diluted": "Diluted EPS",
              "operating_cash_flow": "Operating cash flow"}
    for key, label in labels.items():
        if fundamentals.get(key):
            financials.append(_with_change(label, fundamentals[key]))
    if fundamentals.get("long_term_debt"):
        debt = fundamentals["long_term_debt"]
        financials.append(f"Long-term debt: {_money(debt['value'])} (as of {debt['period_end']}).")
    if not financials:
        financials.append("No SEC financial statements are available for this company.")

    risks = []
    if news["negative"]:
        risks.append(f"{news['negative']} negative headline{'s' if news['negative'] != 1 else ''} in recent news.")
    if trend is not None and trend < 0:
        risks.append("The 90-day trend is down.")
    if day is not None and abs(day) >= BIG_DAY_MOVE_PCT:
        risks.append(f"A {abs(day):.1f}% move in one day: the price is swinging a lot right now.")
    net_income = fundamentals.get("net_income")
    if net_income and net_income["value"] < 0:
        risks.append("The company lost money in its latest reported year.")
    revenue, debt = fundamentals.get("revenue"), fundamentals.get("long_term_debt")
    if revenue and debt and revenue["value"] > 0 and debt["value"] > revenue["value"]:
        risks.append("Long-term debt is larger than a full year of revenue.")
    if facts.get("market_cap") and facts["market_cap"] < SMALL_CAP:
        risks.append("A smaller company: its price can move more sharply than large companies'.")
    if news["company_mentions"] and news["tone_is_keyword_estimate"]:
        risks.append("Headline tone is a keyword estimate (no AI key), so treat it as rough.")
    if not risks:
        risks.append("No specific warning signs in the data the app has. That is not a full analysis.")

    return StockExplanation(
        summary=summary, why_moving=why, trend=trend_points, financials=financials, risks=risks,
        bottom_line=f"{facts['signal']}. {DISCLAIMER}",
    )


# -- Claude explanation --------------------------------------------------------

SYSTEM_PROMPT = """You explain one stock to someone new to investing, using \
only the JSON facts you are given. Write short, plain sentences and quote \
the numbers from the facts (prices, percentages, headline titles). Do not \
add anything you were not given, such as recent events, analyst views or \
price targets, even if you know them; the facts are the app's current data \
and your own knowledge may be out of date.

Fill each field:
- summary: two or three sentences on what is going on with the stock.
- why_moving: what the headlines (company and sector-wide) and the sector's \
move today say about the price move. If nothing explains it, say so.
- trend: the 90-day trend, against the S&P 500 and against its sector.
- financials: revenue, profit, cash flow and debt in plain words, with the \
year-on-year changes. If fundamentals are missing, say they are unavailable.
- risks: concrete things to watch that the facts support.
- bottom_line: one sentence that sums up the picture.

Never tell the reader to buy, sell or hold. Only when tone_is_keyword_estimate \
is true, mention once that headline tone is a rough keyword estimate; when \
it is false, don't bring it up."""


def explain_with_claude(client, facts):
    """StockExplanation written by Claude, or None if it declined or the
    reply didn't validate (the caller falls back to the rules)."""
    response = client.beta.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # re-run on a fallback model if this one declines
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(facts, default=str)}],
        output_format=StockExplanation,
    )
    if response.stop_reason == "refusal":
        return None
    explanation = response.parsed_output
    if explanation is not None:
        explanation.bottom_line = f"{explanation.bottom_line.rstrip()} {DISCLAIMER}"
    return explanation
