"""
market_data.py

Keeps a live, ranked momentum snapshot of the largest US-listed stocks
(top 3000 by market cap by default, roughly the Russell 3000) so API
requests never wait on a download.

Two sources, refreshed on different clocks by a background thread:
    - Nasdaq's stock screener (one request, all US listings): the universe
      itself plus name, sector, market cap, last price, daily change and
      volume. Refreshed every 5 minutes. Prices are delayed ~15 minutes.
    - yfinance daily history: the price at the start of the lookback window
      for each stock, plus the S&P 500 for relative strength. That start
      price barely changes intraday, so this only refreshes every 6 hours
      (a full 3000-stock download takes ~1-2 minutes).

momentum_pct = latest screener price vs. the lookback-start price, so
rankings move with every quote refresh without re-downloading history.

Everything is saved to market_cache.json, so a restart serves data
immediately; only the very first run waits for the initial download.

Config (env vars):
    MARKET_UNIVERSE_SIZE   number of stocks to track (default 3000)
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from momentum.momentum_engine import BENCHMARK_TICKER, DEFAULT_SECTOR_LOOKUP, DEFAULT_UNIVERSE

log = logging.getLogger(__name__)

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
SCREENER_HEADERS = {
    # The screener rejects requests without a browser-like user agent.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
}
QUOTE_REFRESH_SECONDS = 5 * 60
HISTORY_REFRESH_SECONDS = 6 * 60 * 60
HISTORY_RETRY_SECONDS = 15 * 60  # after a failed history download
MIN_HISTORY_COVERAGE = 0.5       # a download with less than this is treated as failed
HISTORY_CHUNK_SIZE = 500
DEFAULT_CACHE_PATH = Path(__file__).parent / "market_cache.json"

# Nasdaq's sector names, mapped onto the names the rest of the app uses.
SECTOR_NAMES = {"Finance": "Financials", "Health Care": "Healthcare", "": "Unknown"}

# Common stock and share classes only ("BRK/B" is Berkshire class B);
# skips preferreds ("ABR^D"), warrants and units, which use other forms.
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(/[A-Z])?$")
# ...and the symbol pattern alone isn't enough: Nasdaq gives preferreds and
# bank depositary shares 5-letter symbols too (e.g. ZIONP, GOOGM), so also
# drop anything whose name says it isn't common stock. Foreign companies'
# "American Depositary Shares" (ADRs) and partnerships' "Common Units" stay.
_NON_COMMON_RE = re.compile(
    r"\b(preferred|warrants?|rights?|notes? due|debentures?|(?<!common )units?|(?<!american )deposit[ao]ry shares?)\b",
    re.IGNORECASE,
)
# ADR descriptions can mention preferred shares or units they represent
# ("...American Depositary Shares (each representing 500 Preferred...)").
_ADR_RE = re.compile(r"american depositary", re.IGNORECASE)
_NAME_SUFFIX_RE = re.compile(
    r"\s+(Class [A-Z] )?(Common Stock|Common Shares|Ordinary Shares|Subordinate Voting Shares"
    r"|American Depositary Shares|Sponsored ADR)\b.*$",
    re.IGNORECASE,
)


def _to_float(value):
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def fetch_universe(limit):
    """Returns the `limit` largest US-listed common stocks by market cap,
    with name, sector, and the latest (delayed) quote for each."""
    res = requests.get(SCREENER_URL, headers=SCREENER_HEADERS, timeout=30)
    res.raise_for_status()
    rows = res.json()["data"]["rows"]

    stocks = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        market_cap = _to_float(row.get("marketCap"))
        price = _to_float(row.get("lastsale"))
        if not _SYMBOL_RE.match(symbol) or not market_cap or not price:
            continue
        name = row.get("name") or ""
        if _NON_COMMON_RE.search(name) and not _ADR_RE.search(name):
            continue
        sector = (row.get("sector") or "").strip()
        stocks.append({
            "ticker": symbol.replace("/", "-"),  # yfinance spells BRK/B as BRK-B
            "name": _NAME_SUFFIX_RE.sub("", (row.get("name") or symbol).strip()) or symbol,
            "sector": SECTOR_NAMES.get(sector, sector),
            "industry": (row.get("industry") or "").strip(),
            "market_cap": market_cap,
            "current_price": price,
            "day_change_pct": _to_float(row.get("pctchange")),
            "volume": int(_to_float(row.get("volume")) or 0),
        })

    stocks.sort(key=lambda s: s["market_cap"], reverse=True)
    return stocks[:limit]


def fetch_lookback_prices(tickers, lookback_days):
    """Returns {ticker: {"start": price at lookback start, "last": latest close}}
    from daily history, downloaded in chunks so one bad batch doesn't sink
    the rest."""
    prices = {}
    period = f"{lookback_days + 10}d"  # buffer for weekends/holidays
    for i in range(0, len(tickers), HISTORY_CHUNK_SIZE):
        chunk = tickers[i:i + HISTORY_CHUNK_SIZE]
        try:
            data = yf.download(chunk, period=period, progress=False, auto_adjust=True, threads=True)
        except Exception as e:  # network errors, rate limits
            log.warning("History download failed for %d tickers: %s", len(chunk), e)
            continue
        if data.empty:
            continue
        closes = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]].set_axis(chunk, axis=1)
        for ticker in closes.columns:
            series = closes[ticker].dropna()
            if len(series) < 2:
                continue
            start = float(series.iloc[max(0, len(series) - lookback_days)])
            if start > 0:
                prices[ticker] = {"start": round(start, 4), "last": round(float(series.iloc[-1]), 4)}
    return prices


class MarketData:
    def __init__(self, cache_path=DEFAULT_CACHE_PATH, universe_size=None, lookback_days=90):
        self.cache_path = Path(cache_path)
        self.universe_size = universe_size or int(os.environ.get("MARKET_UNIVERSE_SIZE", "3000"))
        self.lookback_days = lookback_days
        self._lock = threading.Lock()
        self._universe = []        # screener rows, largest first
        self._history = {}         # ticker -> {"start", "last"}
        self._no_history = set()   # tickers yfinance had no data for (don't retry every loop)
        self._rows = []            # ranked snapshot served to the API
        self._by_ticker = {}
        self._sectors = []
        self.quotes_updated_at = None
        self.history_updated_at = None
        self.last_error = None
        self._history_retry_at = 0.0  # time.monotonic() before which not to retry
        self._thread = None

    # -- reading -----------------------------------------------------------

    def snapshot(self):
        """Ranked rows, best momentum first. The list is replaced wholesale on
        each refresh, so callers can read it without locking."""
        return self._rows

    def sectors(self):
        return self._sectors

    def lookup(self, ticker):
        return self._by_ticker.get((ticker or "").upper())

    def status(self):
        return {
            "tracked": len(self._rows),
            "universe_size": self.universe_size,
            "quotes_updated_at": self.quotes_updated_at,
            "history_updated_at": self.history_updated_at,
            "warming_up": not self._rows,
            "last_error": self.last_error,
        }

    # -- refreshing --------------------------------------------------------

    def refresh_quotes(self):
        try:
            universe = fetch_universe(self.universe_size)
        except Exception as e:
            self.last_error = f"Quote refresh failed: {e}"
            log.warning(self.last_error)
            if self._universe:
                return  # keep serving the last good universe
            # No screener and no cache: fall back to the built-in starter list.
            universe = [{"ticker": t, "name": t, "sector": DEFAULT_SECTOR_LOOKUP.get(t, "Unknown"),
                         "industry": "", "market_cap": 0, "current_price": None,
                         "day_change_pct": None, "volume": 0} for t in DEFAULT_UNIVERSE]
        else:
            self.last_error = None
        with self._lock:
            self._universe = universe
            self.quotes_updated_at = _now()
            self._rebuild()
        self._save()

    def refresh_history(self, tickers=None):
        full = tickers is None
        tickers = tickers or [s["ticker"] for s in self._universe]
        prices = fetch_lookback_prices(tickers + [BENCHMARK_TICKER], self.lookback_days)
        if full and len(prices) < len(tickers) * MIN_HISTORY_COVERAGE:
            # Yahoo down or rate-limiting: keep serving the history we have
            # rather than replacing it with a near-empty one for 6 hours.
            self.last_error = f"History refresh got {len(prices)} of {len(tickers)} stocks; retrying in {HISTORY_RETRY_SECONDS // 60} min"
            log.warning(self.last_error)
            self._history_retry_at = time.monotonic() + HISTORY_RETRY_SECONDS
            return
        with self._lock:
            if full:
                # Keep the previous values for stocks this download happened to miss.
                current = set(tickers) | {BENCHMARK_TICKER}
                self._history = {t: h for t, h in self._history.items() if t in current}
                self._history.update(prices)
                self._no_history = set(tickers) - set(self._history)
                self.history_updated_at = _now()
                self.last_error = None
            else:
                self._history.update(prices)
                self._no_history |= set(tickers) - set(prices)
            self._rebuild()
        self._save()

    def _rebuild(self):
        benchmark = self._history.get(BENCHMARK_TICKER)
        benchmark_pct = (benchmark["last"] - benchmark["start"]) / benchmark["start"] * 100 if benchmark else None

        rows = []
        for stock in self._universe:
            history = self._history.get(stock["ticker"])
            price = stock["current_price"] or (history or {}).get("last")
            if not history or not price:
                continue
            momentum = (price - history["start"]) / history["start"] * 100
            row = {**stock, "current_price": round(price, 2), "momentum_pct": round(momentum, 2)}
            if benchmark_pct is not None:
                row["relative_strength"] = round(momentum - benchmark_pct, 2)
            rows.append(row)

        rows.sort(key=lambda r: r["momentum_pct"], reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        self._rows = rows
        self._by_ticker = {row["ticker"]: row for row in rows}
        self._sectors = sorted({r["sector"] for r in rows})

    # -- background loop ---------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._load()
        self._thread = threading.Thread(target=self._run, name="market-data", daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            try:
                if _age(self.quotes_updated_at) > QUOTE_REFRESH_SECONDS:
                    self.refresh_quotes()
                if _age(self.history_updated_at) > HISTORY_REFRESH_SECONDS:
                    if time.monotonic() >= self._history_retry_at:
                        self.refresh_history()
                else:
                    # Stocks that joined the universe since the last full download.
                    missing = [s["ticker"] for s in self._universe
                               if s["ticker"] not in self._history and s["ticker"] not in self._no_history]
                    if missing:
                        self.refresh_history(missing)
            except Exception as e:
                self.last_error = f"Market data refresh failed: {e}"
                log.exception(self.last_error)
            time.sleep(30)

    def _load(self):
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return
        with self._lock:
            self._universe = data.get("universe", [])
            self._history = data.get("history", {})
            self._no_history = set(data.get("no_history", []))
            self.quotes_updated_at = data.get("quotes_updated_at")
            self.history_updated_at = data.get("history_updated_at")
            self._rebuild()

    def _save(self):
        data = {
            "universe": self._universe,
            "history": self._history,
            "no_history": sorted(self._no_history),
            "quotes_updated_at": self.quotes_updated_at,
            "history_updated_at": self.history_updated_at,
        }
        tmp = self.cache_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data))
            tmp.replace(self.cache_path)
        except OSError as e:
            log.warning("Could not save market cache: %s", e)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _age(timestamp):
    if not timestamp:
        return float("inf")
    return (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()
