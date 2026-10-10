"""
autopilot.py

Decides what the paper-trading autopilot should buy and sell, with the reason
for each trade. Pure function: no database, no network. api/main.py gathers
the inputs (holdings, live quotes, news) and places the trades.

Rules (paper money only, stocks only):
    - The autopilot manages only the positions it bought itself; the user's
      own holdings and options are never touched.
    - Kill switch: if the portfolio is down daily_loss_limit_pct or more from
      its last recorded close, it pauses itself and trades nothing.
    - Sells, for positions it manages: stop-loss hit, 90-day trend turned
      negative, fell far behind the S&P 500, or negative news while lagging it.
    - Buys, from the largest stocks: rising over 90 days and beating the
      S&P 500, never with negative news; stocks with good news on a rising
      trend first, then the strongest against the S&P 500. Equal-sized
      positions, capped per stock, always keeping a cash reserve.
"""

from paper_trading.paper_trading_engine import MIN_ORDER_VALUE, order_value, shares_for_amount

# Settings: default, minimum, maximum. The user can change these per portfolio.
SETTINGS = {
    "max_positions": (8, 1, 20),          # stocks the autopilot holds at once
    "max_position_pct": (15, 1, 50),      # most of the portfolio in one stock
    "cash_reserve_pct": (5, 0, 50),       # cash it never spends
    "stop_loss_pct": (12, 1, 50),         # sell a position down this much from cost
    "daily_loss_limit_pct": (5, 1, 50),   # pause itself after a drop this big
    "max_trades_per_run": (10, 1, 30),
}
DEFAULT_SETTINGS = {key: default for key, (default, _, _) in SETTINGS.items()} | {"use_news": True}

LAGGING_EXIT_POINTS = -10  # sell when this many points behind the S&P 500 over 90 days


def clamp_settings(settings):
    """Defaults for anything missing, and every number kept inside its range."""
    out = dict(DEFAULT_SETTINGS)
    for key, value in (settings or {}).items():
        if key in SETTINGS:
            _, low, high = SETTINGS[key]
            out[key] = max(low, min(high, int(value)))
        elif key == "use_news":
            out[key] = bool(value)
    return out


def _pct(value):
    return f"{value:+.1f}%"


def plan_run(settings, cash, total_value, holdings, managed, lookup, universe, news=None, reference_value=None):
    """
    settings: as from clamp_settings
    cash, total_value: the portfolio's cash and total value right now
    holdings: stock holdings [{"ticker", "quantity", "avg_buy_price"}], all of them
    managed: tickers the autopilot bought and still manages
    lookup(ticker): the tracked market row ({"current_price", "momentum_pct",
        "relative_strength", "sector", ...}) or None
    universe: market rows to pick buys from (the largest stocks)
    news: {ticker: {"tone_label": "Positive"|"Negative"|..., "view": "Good"|...}},
        or None when headlines aren't available
    reference_value: the portfolio's last recorded close, for the kill switch

    Returns {"status": "ok"|"paused"|"idle", "trades": [...], "notes": [...]}.
    Sells come first; each trade has side, ticker, quantity, price, value, reason.
    """
    s = clamp_settings(settings)
    notes, trades = [], []
    use_news = s["use_news"] and news is not None
    if s["use_news"] and news is None:
        notes.append("News isn't available right now, so this run uses price trends only.")
    news = news or {}

    if reference_value and total_value < reference_value * (1 - s["daily_loss_limit_pct"] / 100):
        drop = (total_value / reference_value - 1) * 100
        return {"status": "paused", "trades": [], "notes": notes + [
            f"Daily loss limit hit: the portfolio is {_pct(drop)} since its last close "
            f"(limit -{s['daily_loss_limit_pct']}%). Autopilot paused itself; turn it back on to resume."]}

    held = {h["ticker"]: h for h in holdings}
    managed = [t for t in managed if t in held]
    budget_trades = s["max_trades_per_run"]

    # --- Sells: only positions the autopilot manages --------------------
    selling = set()
    for ticker in managed:
        if len(trades) >= budget_trades:
            break
        h, row = held[ticker], lookup(ticker)
        if not row or not row.get("current_price"):
            notes.append(f"No live price for {ticker}; keeping it this run.")
            continue
        price = float(row["current_price"])
        change = (price / float(h["avg_buy_price"]) - 1) * 100
        momentum, strength = row.get("momentum_pct"), row.get("relative_strength")
        tone = news.get(ticker, {}).get("tone_label") if use_news else None
        if change <= -s["stop_loss_pct"]:
            reason = f"Stop-loss: {_pct(change)} from your cost of ${float(h['avg_buy_price']):,.2f}"
        elif momentum is not None and momentum < 0:
            reason = f"Trend turned down: {_pct(momentum)} over 90 days"
        elif strength is not None and strength <= LAGGING_EXIT_POINTS:
            reason = f"Fell {abs(strength):.1f} points behind the S&P 500 over 90 days"
        elif tone == "Negative" and (strength or 0) < 0:
            reason = "Negative news while trailing the S&P 500"
        else:
            continue
        quantity = float(h["quantity"])
        trades.append({"side": "SELL", "ticker": ticker, "quantity": quantity, "price": price,
                       "value": order_value(quantity, price, "SELL"), "sector": row.get("sector"), "reason": reason})
        selling.add(ticker)

    # --- Buys: fill open slots with the strongest stocks ----------------
    cash_after_sells = cash + sum(t["value"] for t in trades)
    available = cash_after_sells - total_value * s["cash_reserve_pct"] / 100
    slots = s["max_positions"] - (len(managed) - len(selling))
    if slots <= 0:
        notes.append(f"Already holding {s['max_positions']} autopilot positions (the maximum).")
    elif available < MIN_ORDER_VALUE:
        notes.append(f"Not enough cash above the {s['cash_reserve_pct']}% reserve to buy anything.")
    else:
        candidates = []
        for row in universe:
            ticker, price = row.get("ticker"), row.get("current_price")
            momentum, strength = row.get("momentum_pct"), row.get("relative_strength")
            if not price or price <= 0 or ticker in held or momentum is None or strength is None:
                continue
            if momentum <= 0 or strength <= 0:
                continue
            item = news.get(ticker, {}) if use_news else {}
            if item.get("tone_label") == "Negative":
                continue
            candidates.append((item.get("view") == "Good", strength, row))
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)

        per_stock_cap = total_value * s["max_position_pct"] / 100
        target = min(per_stock_cap, available / slots)
        for good_news, strength, row in candidates:
            if slots <= 0 or len(trades) >= budget_trades or available < MIN_ORDER_VALUE:
                break
            price = float(row["current_price"])
            quantity = shares_for_amount(min(target, available), price)
            value = order_value(quantity, price, "BUY")
            if value < MIN_ORDER_VALUE:
                continue
            reason = (f"{_pct(row['momentum_pct'])} over 90 days, {strength:+.1f} points ahead of the S&P 500"
                      + ("; good news on a rising trend" if good_news else ""))
            trades.append({"side": "BUY", "ticker": row["ticker"], "quantity": quantity, "price": price,
                           "value": value, "sector": row.get("sector"), "reason": reason})
            available -= value
            slots -= 1
        if not any(t["side"] == "BUY" for t in trades) and slots > 0:
            notes.append("No stock passed the buy rules right now (rising, beating the S&P 500, no negative news).")

    return {"status": "ok" if trades else "idle", "trades": trades, "notes": notes}
