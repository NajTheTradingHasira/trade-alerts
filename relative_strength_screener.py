#!/usr/bin/env python3
"""
Relative Strength Screener — Up on Down Days (Oliver Kell)

Finds stocks showing institutional support by closing UP on days
when the broad market (S&P 500) is DOWN:
  - Index (^GSPC) must be down on the day
  - Stock closes up on the day
  - Price above both 20-day and 50-day SMA (confirmed uptrend)
  - Price ≥ $20, avg 20-day volume ≥ 500K

Universe: S&P 500 + Nasdaq 100 + Dow 30 + Russell 2000 (deduplicated)

Usage:
  python relative_strength_screener.py
  python relative_strength_screener.py --index ^NDX     # use Nasdaq as benchmark
  python relative_strength_screener.py --lookback 5     # check last 5 down days
  python relative_strength_screener.py --slack --config channel_config.json

Route key emitted: __relative_strength__ → #relative-strength
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Import shared universe loader
# ---------------------------------------------------------------------------
try:
    from growth_screener import load_universe
except ImportError:
    def load_universe(indexes=None, cache_path="universe_cache.json"):
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        tickers = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"  Fallback universe: {len(tickers)} S&P 500 tickers")
        return tickers


# ---------------------------------------------------------------------------
# Index check — is today a down day?
# ---------------------------------------------------------------------------

def get_index_down_days(
    index_symbol: str = "^GSPC",
    lookback: int = 1,
) -> list[dict]:
    """
    Check recent trading days for index down days.

    Args:
        index_symbol: benchmark index (default ^GSPC = S&P 500)
        lookback: number of recent days to check

    Returns:
        List of dicts with date, close, prev_close, pct_change for down days only.
        Empty list if no down days in the lookback window.
    """
    idx = yf.download(index_symbol, period="1mo", interval="1d", auto_adjust=False, progress=False)

    # Handle multi-level columns
    if isinstance(idx.columns, pd.MultiIndex):
        idx.columns = idx.columns.get_level_values(0)

    idx = idx.dropna()
    if len(idx) < 2:
        return []

    idx["PrevClose"] = idx["Close"].shift(1)
    idx["PctChange"] = (idx["Close"] / idx["PrevClose"] - 1) * 100.0

    down_days = []
    check_rows = idx.tail(lookback)

    for dt, row in check_rows.iterrows():
        if pd.isna(row["PctChange"]):
            continue
        if row["PctChange"] < 0:
            down_days.append({
                "date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt),
                "index_close": round(row["Close"], 2),
                "index_prev_close": round(row["PrevClose"], 2),
                "index_pct_change": round(row["PctChange"], 2),
            })

    return down_days


# ---------------------------------------------------------------------------
# Single-ticker RS detection
# ---------------------------------------------------------------------------

def detect_relative_strength(
    ticker: str,
    down_dates: list[str],
    min_price: float = 20.0,
    min_avg_vol: float = 500_000,
) -> list[dict]:
    """
    Detect relative strength events for a single ticker.

    Args:
        ticker: stock symbol
        down_dates: list of date strings (YYYY-MM-DD) that are index down days
        min_price: minimum stock price (default $20)
        min_avg_vol: minimum 20-day avg volume (default 500K)

    Returns:
        List of RS event dicts (empty if no events)
    """
    try:
        data = yf.download(ticker, period="1y", interval="1d", auto_adjust=False, progress=False)

        # Handle multi-level columns
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        if data.empty or len(data) < 60:
            return []

        data = data.dropna().copy()

        # Derived fields
        data["PrevClose"] = data["Close"].shift(1)
        data["SMA20"] = data["Close"].rolling(20).mean()
        data["SMA50"] = data["Close"].rolling(50).mean()
        data["SMA200"] = data["Close"].rolling(200).mean()
        data["AvgVol20"] = data["Volume"].rolling(20).mean()
        data["High252"] = data["High"].rolling(252).max()

        # Relative volume
        data["RelVol"] = data["Volume"] / data["AvgVol20"]

        # Range position
        data["Range"] = data["High"] - data["Low"]
        data["PosInRange"] = (data["Close"] - data["Low"]) / data["Range"].replace(0, np.nan)

        events = []

        for dt_str in down_dates:
            # Find matching date in data
            matches = data.loc[data.index.strftime("%Y-%m-%d") == dt_str]
            if matches.empty:
                continue

            bar = matches.iloc[0]
            price = float(bar["Close"])

            # Price filter
            if price < min_price:
                continue

            # Liquidity filter
            if pd.isna(bar["AvgVol20"]) or bar["AvgVol20"] < min_avg_vol:
                continue

            # Must be UP on the day
            if pd.isna(bar["PrevClose"]) or price <= float(bar["PrevClose"]):
                continue

            # Uptrend filter: above both 20-day and 50-day SMA
            if pd.isna(bar["SMA20"]) or pd.isna(bar["SMA50"]):
                continue
            if not (price >= bar["SMA20"] and price >= bar["SMA50"]):
                continue

            pct_change = (price / float(bar["PrevClose"]) - 1) * 100.0

            # Context metrics
            above_sma200 = bool(not pd.isna(bar["SMA200"]) and price >= bar["SMA200"])
            pct_from_high = (price / bar["High252"] * 100.0) if not pd.isna(bar["High252"]) else None
            rel_vol = float(bar["RelVol"]) if not pd.isna(bar["RelVol"]) else None
            pos_in_range = float(bar["PosInRange"]) if not pd.isna(bar["PosInRange"]) else None
            dollar_vol = price * bar["Volume"]

            events.append({
                "ticker": ticker,
                "date": dt_str,
                "close": round(price, 2),
                "pct_change": round(pct_change, 2),
                "volume": int(bar["Volume"]),
                "avg_vol_20": int(bar["AvgVol20"]),
                "rel_vol": round(rel_vol, 1) if rel_vol else None,
                "sma_20": round(bar["SMA20"], 2),
                "sma_50": round(bar["SMA50"], 2),
                "above_sma200": above_sma200,
                "pct_from_high": round(pct_from_high, 1) if pct_from_high else None,
                "pos_in_range": round(pos_in_range, 2) if pos_in_range else None,
                "dollar_vol": round(dollar_vol, 0),
                "event_label": "__relative_strength__",
            })

        return events

    except Exception:
        return []


# ---------------------------------------------------------------------------
# Full screener
# ---------------------------------------------------------------------------

def run_relative_strength_screen(
    index_symbol: str = "^GSPC",
    min_price: float = 20.0,
    min_avg_vol: float = 500_000,
    lookback: int = 1,
    force_run: bool = False,
    indexes: list[str] | None = None,
    batch_size: int = 50,
) -> tuple[pd.DataFrame, list[dict]]:
    """
    Run the relative strength screener.

    Only scans on index down days unless force_run is True.

    Returns:
        Tuple of (results DataFrame, list of down day dicts)
    """
    print("=" * 60)
    print("RELATIVE STRENGTH SCREENER — Up on Down Days")
    print(f"  Benchmark: {index_symbol}")
    print(f"  Min price: ${min_price}")
    print(f"  Min avg volume: {min_avg_vol:,.0f}")
    print(f"  Lookback: {lookback} trading day(s)")
    print("=" * 60)

    # Check for index down days
    print(f"\n📉 Checking {index_symbol} for down days...")
    down_days = get_index_down_days(index_symbol, lookback)

    if not down_days and not force_run:
        print(f"\n⚠ No index down days in the last {lookback} trading day(s).")
        print(f"  {index_symbol} was positive — RS scan conditions not met.")
        print(f"  Use --force to run anyway, or increase --lookback.")
        return pd.DataFrame(), down_days

    if not down_days and force_run:
        print(f"\n⚠ No down days found, but --force is set. Using last {lookback} day(s).")
        # Use last N dates regardless
        idx = yf.download(index_symbol, period="1mo", interval="1d", auto_adjust=False, progress=False)
        if isinstance(idx.columns, pd.MultiIndex):
            idx.columns = idx.columns.get_level_values(0)
        idx = idx.dropna()
        idx["PrevClose"] = idx["Close"].shift(1)
        idx["PctChange"] = (idx["Close"] / idx["PrevClose"] - 1) * 100.0
        for dt, row in idx.tail(lookback).iterrows():
            down_days.append({
                "date": dt.strftime("%Y-%m-%d"),
                "index_close": round(row["Close"], 2),
                "index_prev_close": round(row["PrevClose"], 2) if not pd.isna(row["PrevClose"]) else 0,
                "index_pct_change": round(row["PctChange"], 2) if not pd.isna(row["PctChange"]) else 0,
            })

    for dd in down_days:
        print(f"  📅 {dd['date']}: {index_symbol} {dd['index_pct_change']:+.2f}%")

    down_dates = [dd["date"] for dd in down_days]

    # Load universe
    print("\n📡 Loading universe...")
    tickers = load_universe(indexes)

    # Screen
    all_events = []
    errors = 0

    print(f"🔍 Scanning {len(tickers)} tickers for relative strength...\n")

    for i, t in enumerate(tickers):
        if (i + 1) % batch_size == 0:
            print(f"  Progress: {i + 1}/{len(tickers)} "
                  f"({len(all_events)} RS leaders, {errors} errors)")

        events = detect_relative_strength(
            t,
            down_dates=down_dates,
            min_price=min_price,
            min_avg_vol=min_avg_vol,
        )

        if events:
            all_events.extend(events)
        elif events is None:
            errors += 1

    df = pd.DataFrame(all_events)

    if df.empty:
        print(f"\n⚠ No relative strength leaders found.")
        return df, down_days

    # Merge index change into results
    idx_map = {dd["date"]: dd["index_pct_change"] for dd in down_days}
    df["index_pct_change"] = df["date"].map(idx_map)

    # RS spread = stock change minus index change (higher = stronger RS)
    df["rs_spread"] = df["pct_change"] - df["index_pct_change"]

    # Sort by RS spread (strongest relative performance first)
    df = df.sort_values("rs_spread", ascending=False).reset_index(drop=True)

    # RS score
    df["rs_score"] = (
        (df["rs_spread"] / 2.0).clip(upper=4.0) * 3            # spread contribution (max 12)
        + (df["pct_change"] / 1.0).clip(upper=3.0) * 2         # absolute move (max 6)
        + df["above_sma200"].astype(float) * 2                   # long-term trend bonus
        + df["pos_in_range"].fillna(0.5).clip(upper=1.0) * 1    # close quality (max 1)
    ).round(1)

    df = df.sort_values("rs_score", ascending=False).reset_index(drop=True)

    print(f"\n{'=' * 60}")
    print(f"RESULTS — {len(df)} relative strength leaders")
    print(f"  Down day(s): {', '.join(down_dates)}")
    print(f"  Scanned: {len(tickers)} | Found: {len(df)} | Errors: {errors}")
    print(f"{'=' * 60}\n")

    return df, down_days


# ---------------------------------------------------------------------------
# Slack integration
# ---------------------------------------------------------------------------

def format_rs_alert(row: dict) -> str:
    """Format a single RS event as a Slack message."""
    sma200_tag = " ✅ >200d" if row.get("above_sma200") else ""
    high_tag = f" | {row['pct_from_high']:.0f}% of 52wk high" if row.get("pct_from_high") else ""
    return (
        f"💪 *{row['ticker']}* — RS Leader (Up on Down Day){sma200_tag}\n"
        f"> Stock: +{row['pct_change']:.2f}% vs Index: {row['index_pct_change']:.2f}% | "
        f"Spread: +{row['rs_spread']:.2f}%\n"
        f"> Close: ${row['close']:.2f}{high_tag}"
    )


def send_to_slack(
    df: pd.DataFrame,
    down_days: list[dict],
    config_path: str,
    bot_token: str | None = None,
):
    """Send RS results to Slack via router."""
    try:
        from slack_router import SlackRouter
    except ImportError:
        print("  ⚠ slack_router.py not found — skipping Slack delivery")
        return

    router = SlackRouter.from_config(config_path, bot_token)
    route_key = "__relative_strength__"

    channel_id = router._resolve_channel(route_key)
    if not channel_id:
        print(f"  ⚠ No channel configured for {route_key}")
        return

    # Header with index context
    dd_summary = " | ".join([f"{dd['date']}: {dd['index_pct_change']:+.2f}%" for dd in down_days])
    header = (
        f"💪 *RELATIVE STRENGTH SCAN — {datetime.now().strftime('%Y-%m-%d')}*\n"
        f"> {len(df)} stocks UP on index down day(s)\n"
        f"> Index: {dd_summary}"
    )
    router._post_message(channel_id, header)
    router._post_message(router.firehose_id, header)

    # Individual alerts (top 30)
    for _, row in df.head(30).iterrows():
        msg = format_rs_alert(row.to_dict())
        router._post_message(channel_id, msg)
        router._post_message(router.firehose_id, msg)

    if len(df) > 30:
        overflow = f"> +{len(df) - 30} more RS leaders — see CSV for full list"
        router._post_message(channel_id, overflow)

    print(f"  ✓ Sent {min(len(df), 30)} RS alerts to #relative-strength")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Relative Strength Screener — Up on Down Days")
    parser.add_argument("--index", default="^GSPC",
                        help="Benchmark index symbol (default: ^GSPC)")
    parser.add_argument("--min-price", type=float, default=20.0,
                        help="Min stock price (default: $20)")
    parser.add_argument("--min-avg-vol", type=float, default=500_000,
                        help="Min 20-day avg volume (default: 500K)")
    parser.add_argument("--lookback", type=int, default=1,
                        help="Recent trading days to check for down days (default: 1)")
    parser.add_argument("--force", action="store_true",
                        help="Run even if index is not down (scan all days in lookback)")
    parser.add_argument("--indexes", nargs="+",
                        default=["sp500", "nasdaq100", "dow30", "russell2000"],
                        help="Stock universe indexes (default: all)")
    parser.add_argument("--output", default=None,
                        help="Output CSV path")
    parser.add_argument("--slack", action="store_true",
                        help="Send results to Slack via router")
    parser.add_argument("--config", default="channel_config.json",
                        help="Channel config path")
    parser.add_argument("--bot-token", default=None,
                        help="Slack bot token (or set SLACK_BOT_TOKEN)")
    parser.add_argument("--top", type=int, default=50,
                        help="Print top N results (default: 50)")
    args = parser.parse_args()

    # Run screen
    df, down_days = run_relative_strength_screen(
        index_symbol=args.index,
        min_price=args.min_price,
        min_avg_vol=args.min_avg_vol,
        lookback=args.lookback,
        force_run=args.force,
        indexes=args.indexes,
    )

    if df.empty:
        return

    # Console output
    display_cols = [
        "ticker", "date", "close", "pct_change", "index_pct_change",
        "rs_spread", "above_sma200", "pct_from_high", "rs_score",
    ]
    avail = [c for c in display_cols if c in df.columns]
    print(df[avail].head(args.top).to_string(index=False))

    # Save CSV
    outpath = args.output or f"relative_strength_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(outpath, index=False)
    print(f"\n✓ Saved {outpath} ({len(df)} rows)")

    # Slack
    if args.slack:
        print("\n📤 Sending to Slack...")
        send_to_slack(df, down_days, args.config, args.bot_token)


if __name__ == "__main__":
    main()
