"""
sec_edgar.py

Company fundamentals and recent filings from SEC EDGAR (free, no API key).

    - Ticker -> CIK map:  https://www.sec.gov/files/company_tickers.json
    - Financial facts:    https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
    - Filing list:        https://data.sec.gov/submissions/CIK##########.json

SEC requires a User-Agent naming the app and a contact email, and allows at
most 10 requests per second. Set SEC_USER_AGENT, e.g.
    export SEC_USER_AGENT="Mahokshahvata you@example.com"

Normalizing XBRL facts:
    - Companies report the same metric under different tags (Apple's revenue
      moved from `Revenues` to `RevenueFromContractWithCustomer...`), so each
      metric tries a list of tags and uses whichever has the newest data.
    - The same period is repeated in later filings; per period end, the most
      recently filed value wins (it includes restatements).
    - Annual vs quarterly is decided by the period length, not the form, since
      a 10-K also contains quarterly figures and 10-Q cash flow is year-to-date.
    - Foreign filers use IFRS tags and may report in several currencies; USD is
      preferred when present, and the currency is returned with each value.
"""

import os
import threading
import time
from datetime import date

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

DEFAULT_USER_AGENT = "Mahokshahvata admin@example.com"
CACHE_SECONDS = 6 * 60 * 60   # filings change a few times a quarter
TICKER_MAP_SECONDS = 24 * 60 * 60
YEARS = 4
FILING_FORMS = {"10-K", "10-Q", "8-K", "20-F", "40-F", "6-K"}
MAX_FILINGS = 8
STALE_DAYS = 2 * 365  # a "latest" balance older than this means the company stopped reporting that tag

# metric -> [(taxonomy, tag), ...] in preference order; flow metrics cover a
# period (start..end), balance-sheet metrics are a point in time (end only).
FLOW_METRICS = {
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenuesNetOfInterestExpense"),
        ("us-gaap", "SalesRevenueNet"),
        ("ifrs-full", "Revenue"),
    ],
    "net_income": [
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
        ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
        ("ifrs-full", "ProfitLoss"),
    ],
    "eps_diluted": [
        ("us-gaap", "EarningsPerShareDiluted"),
        ("ifrs-full", "DilutedEarningsLossPerShare"),
    ],
    "operating_cash_flow": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ],
}
INSTANT_METRICS = {
    "long_term_debt": [
        ("us-gaap", "LongTermDebt"),
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("ifrs-full", "LongtermBorrowings"),
    ],
}
# Quarterly cash flow is reported year-to-date, so it's annual-only.
QUARTERLY_METRICS = ("revenue", "net_income", "eps_diluted")

_session = requests.Session()
_cache = {}          # key -> (fetched_at, value)
_cache_lock = threading.Lock()


def _get_json(url):
    headers = {"User-Agent": os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT), "Accept": "application/json"}
    res = _session.get(url, headers=headers, timeout=20)
    res.raise_for_status()
    return res.json()


def _cached(key, max_age, load):
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    value = load()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def lookup_cik(ticker):
    """CIK for a ticker, or None. SEC spells share classes with a dash (BRK-B)."""
    def load():
        data = _get_json(TICKERS_URL)
        return {row["ticker"].upper(): (int(row["cik_str"]), row["title"]) for row in data.values()}
    ticker_map = _cached("tickers", TICKER_MAP_SECONDS, load)
    return ticker_map.get(ticker.upper().replace(".", "-"))


def _days(fact):
    return (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days


def _latest_per_period(facts):
    """One fact per period end: the most recently filed (includes restatements)."""
    best = {}
    for fact in facts:
        current = best.get(fact["end"])
        if current is None or fact["filed"] > current["filed"]:
            best[fact["end"]] = fact
    return sorted(best.values(), key=lambda f: f["end"], reverse=True)


def _pick_units(units):
    """(unit name, facts), preferring USD / USD per share."""
    for preferred in ("USD", "USD/shares"):
        if preferred in units:
            return preferred, units[preferred]
    name = next(iter(units))
    return name, units[name]


def _candidates(facts, tags):
    """The first-listed tag's data unless another tag has newer periods."""
    best = None
    for taxonomy, tag in tags:
        concept = facts.get(taxonomy, {}).get(tag)
        if not concept or not concept.get("units"):
            continue
        unit, rows = _pick_units(concept["units"])
        rows = [r for r in rows if r.get("end") and r.get("val") is not None and r.get("filed")]
        if not rows:
            continue
        newest = max(r["end"] for r in rows)
        if best is None or newest > best[0]:
            best = (newest, unit, rows)
    return (best[1], best[2]) if best else (None, [])


def _series(unit, rows, min_days, max_days, limit):
    periodic = [r for r in rows if r.get("start") and min_days <= _days(r) <= max_days]
    return [
        {"period_end": r["end"], "value": r["val"], "unit": unit, "form": r.get("form"), "filed": r["filed"]}
        for r in _latest_per_period(periodic)[:limit]
    ]


def parse_fundamentals(facts, today=None):
    """Annual and quarterly figures from a companyfacts payload's `facts`."""
    today = today or date.today()
    annual, quarterly, latest = {}, {}, {}
    for metric, tags in FLOW_METRICS.items():
        unit, rows = _candidates(facts, tags)
        annual[metric] = _series(unit, rows, 350, 380, YEARS)
        if metric in QUARTERLY_METRICS:
            quarterly[metric] = _series(unit, rows, 80, 100, YEARS)
    for metric, tags in INSTANT_METRICS.items():
        unit, rows = _candidates(facts, tags)
        points = _latest_per_period([r for r in rows if not r.get("start")])
        fresh = points and (today - date.fromisoformat(points[0]["end"])).days <= STALE_DAYS
        latest[metric] = (
            {"period_end": points[0]["end"], "value": points[0]["val"], "unit": unit, "filed": points[0]["filed"]}
            if fresh else None
        )
    return {"annual": annual, "quarterly": quarterly, "latest": latest}


def parse_filings(cik, submissions, limit=MAX_FILINGS):
    """Recent periodic and current reports, with links to the original documents."""
    recent = submissions.get("filings", {}).get("recent", {})
    filings = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in FILING_FORMS:
            continue
        accession = recent["accessionNumber"][i]
        document = recent["primaryDocument"][i]
        filings.append({
            "form": form,
            "filed": recent["filingDate"][i],
            "period": recent["reportDate"][i] or None,
            "description": recent["primaryDocDescription"][i] or form,
            "url": FILING_URL.format(cik=cik, accession=accession.replace("-", ""), document=document),
        })
        if len(filings) >= limit:
            break
    return filings


def get_fundamentals(ticker):
    """Fundamentals + recent filings for a ticker, cached for CACHE_SECONDS.
    Returns None if SEC has no CIK for it (e.g. some foreign listings)."""
    found = lookup_cik(ticker)
    if not found:
        return None
    cik, name = found

    def load():
        try:
            facts = _get_json(FACTS_URL.format(cik=cik)).get("facts", {})
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
            facts = {}  # registered with SEC but files no XBRL financials
        submissions = _get_json(SUBMISSIONS_URL.format(cik=cik))
        return {
            "ticker": ticker.upper(),
            "cik": cik,
            "company": submissions.get("name") or name,
            **parse_fundamentals(facts),
            "filings": parse_filings(cik, submissions),
            "source": "SEC EDGAR",
        }
    return _cached(f"fundamentals:{cik}", CACHE_SECONDS, load)


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(get_fundamentals(sys.argv[1] if len(sys.argv) > 1 else "AAPL"), indent=2))
