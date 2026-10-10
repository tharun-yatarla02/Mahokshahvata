"""
backtest_intraday.py

How would the autopilot's momentum picks have done during each recent trading
day? For every day with 5-minute data (Yahoo keeps about 60 days), it:

    1. picks stocks with the autopilot's buy rules, using only daily closes
       from before that day: rising over ~100 calendar days and beating the
       S&P 500, strongest against the S&P 500 first (same momentum and
       relative-strength definitions as momentum/market_data.py);
    2. "buys" them in equal amounts at 10:00 New York time (the autopilot's
       earliest run), at the open of the 10:00 bar;
    3. tracks the basket every 5 minutes to the close, next to SPY bought at
       the same moment.

Not modelled: news (past headlines can't be replayed), the app's ~15-minute
quote delay, and the autopilot's sell rules across days (each day starts
fresh). The stock list is today's 500 largest companies, so names that fell
out of the top 500 are missing (survivorship bias).

    python autopilot/backtest_intraday.py --positions 8 --out backtest.json
"""

import argparse
import json
import sys
from datetime import time, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_INDEX = "^GSPC"   # what the app's relative strength uses
BENCHMARK_FUND = "SPY"      # what you'd actually buy intraday
LOOKBACK_CALENDAR_DAYS = 100
ENTRY = time(10, 0)
CHUNK = 100


def closes(tickers, **kwargs):
    """Close prices, tickers as columns, downloaded in chunks."""
    frames = []
    for i in range(0, len(tickers), CHUNK):
        data = yf.download(tickers[i:i + CHUNK], progress=False, auto_adjust=True, threads=True, **kwargs)
        if data.empty:
            continue
        frames.append(data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]].set_axis(tickers[i:i + CHUNK], axis=1))
    return pd.concat(frames, axis=1) if frames else pd.DataFrame()


def bars_5m(tickers):
    """5-minute Open and Close bars in New York time for the last ~60 days."""
    opens, closes_ = [], []
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        data = yf.download(chunk, period="60d", interval="5m", progress=False, auto_adjust=True, threads=True)
        if data.empty:
            continue
        o = data["Open"] if isinstance(data.columns, pd.MultiIndex) else data[["Open"]].set_axis(chunk, axis=1)
        c = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data[["Close"]].set_axis(chunk, axis=1)
        opens.append(o)
        closes_.append(c)
    o, c = pd.concat(opens, axis=1), pd.concat(closes_, axis=1)
    o.index = o.index.tz_convert("America/New_York")
    c.index = c.index.tz_convert("America/New_York")
    return o, c


def picks_for(day, daily, positions):
    """The autopilot's buys for the morning of `day`, from closes before it."""
    before = daily[daily.index.date < day]
    window = before[before.index.date >= day - timedelta(days=LOOKBACK_CALENDAR_DAYS)]
    if len(window) < 2:
        return []
    start, last = window.iloc[0], window.iloc[-1]
    momentum = (last / start - 1) * 100
    bench = momentum.get(BENCHMARK_INDEX)
    if pd.isna(bench):
        return []
    strength = (momentum - bench).drop(BENCHMARK_INDEX, errors="ignore")
    ok = strength[(momentum.drop(BENCHMARK_INDEX, errors="ignore") > 0) & (strength > 0)].dropna()
    return [(t, round(float(momentum[t]), 2), round(float(s), 2)) for t, s in ok.sort_values(ascending=False).head(positions).items()]


def basket_path(day_open, day_close, tickers):
    """Equal-weight % return every 5 minutes from the 10:00 entry, or None."""
    entry_rows = day_open[day_open.index.time == ENTRY]
    if entry_rows.empty:
        return None, []
    entry = entry_rows.iloc[0][tickers].dropna()
    entry = entry[entry > 0]
    held = list(entry.index)
    if not held:
        return None, []
    after = day_close[day_close.index.time >= ENTRY][held].ffill()
    return ((after / entry - 1) * 100).mean(axis=1), held


def run(positions):
    cache = json.loads((ROOT / "momentum" / "market_cache.json").read_text())
    universe = sorted(cache["universe"], key=lambda r: r.get("market_cap") or 0, reverse=True)[:500]
    tickers = [r["ticker"] for r in universe]
    print(f"Daily closes for {len(tickers)} stocks…", file=sys.stderr)
    daily = closes(tickers + [BENCHMARK_INDEX], period="9mo", interval="1d")

    print("5-minute bars for SPY…", file=sys.stderr)
    spy_open, spy_close = bars_5m([BENCHMARK_FUND])
    days = sorted({ts.date() for ts in spy_close.index if ts.time() == ENTRY})

    plan = {day: picks_for(day, daily, positions) for day in days}
    union = sorted({t for picks in plan.values() for t, _, _ in picks})
    print(f"5-minute bars for {len(union)} picked stocks…", file=sys.stderr)
    stock_open, stock_close = bars_5m(union)

    results, paths = [], []
    for day in days:
        picks = plan[day]
        if not picks:
            continue
        on_day = lambda frame: frame[frame.index.date == day]
        path, held = basket_path(on_day(stock_open), on_day(stock_close), [t for t, _, _ in picks])
        spy_path, _ = basket_path(on_day(spy_open), on_day(spy_close), [BENCHMARK_FUND])
        if path is None or spy_path is None:
            continue
        both = pd.DataFrame({"autopilot": path, "spy": spy_path}).dropna()
        last_bar = on_day(stock_close)[held].ffill().iloc[-1]
        entry = on_day(stock_open)[on_day(stock_open).index.time == ENTRY].iloc[0][held]
        per_stock = ((last_bar / entry - 1) * 100).round(2)
        results.append({
            "date": day.isoformat(),
            "picks": [{"ticker": t, "momentum_pct": m, "relative_strength": s, "day_return_pct": float(per_stock.get(t, float("nan")))}
                      for t, m, s in picks if t in held],
            "autopilot_pct": round(float(both["autopilot"].iloc[-1]), 3),
            "spy_pct": round(float(both["spy"].iloc[-1]), 3),
            "autopilot_low_pct": round(float(both["autopilot"].min()), 3),
            "autopilot_high_pct": round(float(both["autopilot"].max()), 3),
        })
        both.index = both.index.strftime("%H:%M")
        paths.append(both)

    avg_path = pd.concat(paths).groupby(level=0).mean().round(3)
    df = pd.DataFrame(results)
    excess = df["autopilot_pct"] - df["spy_pct"]
    summary = {
        "days": len(df), "first_day": df["date"].iloc[0], "last_day": df["date"].iloc[-1], "positions": positions,
        "avg_autopilot_pct": round(df["autopilot_pct"].mean(), 3), "avg_spy_pct": round(df["spy_pct"].mean(), 3),
        "avg_excess_pct": round(excess.mean(), 3),
        "beat_spy_days": int((excess > 0).sum()), "positive_days": int((df["autopilot_pct"] > 0).sum()),
        "total_autopilot_pct": round(((1 + df["autopilot_pct"] / 100).prod() - 1) * 100, 2),
        "total_spy_pct": round(((1 + df["spy_pct"] / 100).prod() - 1) * 100, 2),
        "best_day": df.loc[df["autopilot_pct"].idxmax(), ["date", "autopilot_pct", "spy_pct"]].to_dict(),
        "worst_day": df.loc[df["autopilot_pct"].idxmin(), ["date", "autopilot_pct", "spy_pct"]].to_dict(),
        "avg_intraday_low_pct": round(df["autopilot_low_pct"].mean(), 3),
        "volatility_autopilot_pct": round(df["autopilot_pct"].std(), 3), "volatility_spy_pct": round(df["spy_pct"].std(), 3),
    }
    return {"summary": summary, "days": results,
            "average_path": [{"time": t, "autopilot_pct": r["autopilot"], "spy_pct": r["spy"]} for t, r in avg_path.iterrows()]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--positions", type=int, default=8, help="stocks bought each day (autopilot default: 8)")
    parser.add_argument("--out", type=Path, help="write the full result as JSON")
    args = parser.parse_args()
    result = run(args.positions)
    if args.out:
        args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps(result["summary"], indent=1))


if __name__ == "__main__":
    main()
