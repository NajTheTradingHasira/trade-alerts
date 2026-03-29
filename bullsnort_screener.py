#!/usr/bin/env python3
"""
Bull Snort Screener

Detects institutional accumulation via volume surge + strong close + trend:
  - Volume ≥ 3x 50-day average
  - Close in upper 35% of day's range (PosInRange ≥ 0.65)
  - Up day (close > previous close)
  - Price above 50-day SMA
  - Price within 20% of 52-week high
  - Price ≥ $20, avg 20-day volume ≥ 500K

Universe: S&P 500 + Nasdaq 100 + Dow 30 + Russell 2000 (deduplicated)

Usage:
  python bullsnort_screener.py
  python bullsnort_screener.py --min-vol-multiple 4 --min-pos-in-range 0.70
  python bullsnort_screener.py --slack --config channel_config.json
  python bullsnort_screener.py --lookback 5

Route key emitted: __bullsnort__ → #bullsnort
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
# Single-ticker Bull Snort detection
# ---------------------------------------------------------------------------

def detect_bullsnort(
    ticker: str,
    min_vol_multiple: float = 3.0,
    min_pos_in_range: float = 0.65,
    min_price: float = 20.0,
    min_avg_vol: float = 500_000,
    max_pct_from_high: float = 0.80,
    lookback: int = 1,
) -> list[dict]:
    """
    Detect Bull Snort events for a single ticker.

    Args:
        ticker: stock symbol
        min_vol_multiple: minimum volume vs 50-day avg (default 3x)
        min_pos_in_range: minimum close position in day's range (default 0.65 = top 35%)
        min_price: minimum stock price (default $20)
        min_avg_vol: minimum 20-day avg volume (default 500K)
        max_pct_from_high: minimum price as fraction of 52-week high (default 0.80)
        lookback: number of recent trading days to check (default 1)

    Returns:
        List of Bull Snort event dicts (empty if no events)
    """
    try:
        data = yf.download(ticker, period="14mo", interval="1d", auto_adjust=False, progress=False)

        # Handle multi-level columns
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        if data.empty or len(data) < 60:
            return []

        data = data.dropna().copy()

        # Derived fields
        data["PrevClose"] = data["Close"].shift(1)
        data["Range"] = data["High"] - data["Low"]
        data["PosInRange"] = (data["Close"] - data["Low"]) / data["Range"].replace(0, np.nan)
        data["Vol50"] = data["Volume"].rolling(50).mean()
        data["Vol20"] = data["Volume"].rolling(20).mean()
        data["SMA50"] = data["Close"].rolling(50).mean()
        data["High252"] = data["High"].rolling(252).max()

        events = []
        check_rows = data.tail(lookback)

        for idx, bar in check_rows.iterrows():
            price = float(bar["Close"])

            # Price filter
            if price < min_price:
                continue

            # Liquidity filter
            if pd.isna(bar["Vol20"]) or bar["Vol20"] < min_avg_vol:
                continue

            # Volume surge (core Bull Snort condition)
            if pd.isna(bar["Vol50"]) or bar["Volume"] < min_vol_multiple * bar["Vol50"]:
                continue

            # Strong close — upper portion of range
            if pd.isna(bar["PosInRange"]) or bar["PosInRange"] < min_pos_in_range:
                continue

            # Up day
            if pd.isna(bar["PrevClose"]) or price <= float(bar["PrevClose"]):
                continue

            # Trend context: above 50-day SMA
            if pd.isna(bar["SMA50"]) or price < bar["SMA50"]:
                continue

            # Near 52-week high
            if pd.isna(bar["High252"]) or price < max_pct_from_high * bar["High252"]:
                continue

            # Compute additional context
            vol_multiple = bar["Volume"] / bar["Vol50"]
            day_change_pct = (price - float(bar["PrevClose"])) / float(bar["PrevClose"]) * 100.0
            pct_from_high = price / bar["High252"] * 100.0
            dollar_vol = price * bar["Volume"]

            events.append({
                "ticker": ticker,
                "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx),
                "close": round(price, 2),
                "volume": int(bar["Volume"]),
                "vol_50_avg": int(bar["Vol50"]),
                "vol_multiple": round(vol_multiple, 1),
                "pos_in_range": round(bar["PosInRange"], 2),
                "day_change_pct": round(day_change_pct, 1),
                "sma_50": round(bar["SMA50"], 2),
                "high_252": round(bar["High252"], 2),
                "pct_from_high": round(pct_from_high, 1),
                "dollar_vol": round(dollar_vol, 0),
                "event_label": "__bullsnort__",
            })

        return events

    except Exception:
        return []


# ---------------------------------------------------------------------------
# Full screener
# ---------------------------------------------------------------------------

def run_bullsnort_screen(
    min_vol_multiple: float = 3.0,
    min_pos_in_range: float = 0.65,
    min_price: float = 20.0,
    min_avg_vol: float = 500_000,
    max_pct_from_high: float = 0.80,
    lookback: int = 1,
    indexes: list[str] | None = None,
    batch_size: int = 50,
) -> pd.DataFrame:
    """
    Run the Bull Snort screener across the full universe.

    Returns DataFrame of Bull Snort events sorted by volume multiple.
    """
    print("=" * 60)
    print("BULL SNORT SCREENER")
    print(f"  Min volume multiple: {min_vol_multiple}x (vs 50-day avg)")
    print(f"  Min close position: {min_pos_in_range:.0%} (top {(1 - min_pos_in_range) * 100:.0f}% of range)")
    print(f"  Min price: ${min_price}")
    print(f"  Min avg volume: {min_avg_vol:,.0f}")
    print(f"  Near high threshold: {max_pct_from_high:.0%} of 52-week high")
    print(f"  Lookback: {lookback} trading day(s)")
    print("=" * 60)

    # Load universe
    print("\n📡 Loading universe...")
    tickers = load_universe(indexes)

    # Screen
    all_events = []
    errors = 0

    print(f"🔍 Scanning {len(tickers)} tickers for Bull Snort setups...\n")

    for i, t in enumerate(tickers):
        if (i + 1) % batch_size == 0:
            print(f"  Progress: {i + 1}/{len(tickers)} "
                  f"({len(all_events)} hits, {errors} errors)")

        events = detect_bullsnort(
            t,
            min_vol_multiple=min_vol_multiple,
            min_pos_in_range=min_pos_in_range,
            min_price=min_price,
            min_avg_vol=min_avg_vol,
            max_pct_from_high=max_pct_from_high,
            lookback=lookback,
        )

        if events:
            all_events.extend(events)
        elif events is None:
            errors += 1

    df = pd.DataFrame(all_events)

    if df.empty:
        print(f"\n⚠ No Bull Snort setups detected in the last {lookback} day(s).")
        return df

    # Sort by volume multiple (strongest institutional signal first)
    df = df.sort_values(["vol_multiple", "pos_in_range"], ascending=False).reset_index(drop=True)

    # Score: volume multiple + close position + proximity to high
    df["snort_score"] = (
        (df["vol_multiple"] / 3.0).clip(upper=3.0) * 3      # volume contribution (max 9)
        + (df["pos_in_range"] / 0.65).clip(upper=2.0) * 2   # close position (max 4)
        + (df["pct_from_high"] / 100.0).clip(upper=1.5) * 2  # proximity to high (max 3)
    ).round(1)

    df = df.sort_values("snort_score", ascending=False).reset_index(drop=True)

    print(f"\n{'=' * 60}")
    print(f"RESULTS — {len(df)} Bull Snort setups detected")
    print(f"{'=' * 60}\n")

    return df


# ---------------------------------------------------------------------------
# Slack integration
# ---------------------------------------------------------------------------

def format_bullsnort_alert(row: dict) -> str:
    """Format a single Bull Snort event as a Slack message."""
    return (
        f"🐂 *{row['ticker']}* — Bull Snort\n"
        f"> Vol: {row['vol_multiple']:.1f}x avg | "
        f"Close: ${row['close']:.2f} (+{row['day_change_pct']:.1f}%) | "
        f"Range pos: {row['pos_in_range']:.0%}\n"
        f"> {row['pct_from_high']:.1f}% of 52wk high | "
        f"Dollar vol: ${row['dollar_vol']:,.0f}"
    )


def send_to_slack(df: pd.DataFrame, config_path: str, bot_token: str | None = None):
    """Send Bull Snort results to Slack via router."""
    try:
        from slack_router import SlackRouter
    except ImportError:
        print("  ⚠ slack_router.py not found — skipping Slack delivery")
        return

    router = SlackRouter.from_config(config_path, bot_token)
    route_key = "__bullsnort__"

    channel_id = router._resolve_channel(route_key)
    if not channel_id:
        print(f"  ⚠ No channel configured for {route_key}")
        return

    # Header
    header = (
        f"🐂 *BULL SNORT SCAN — {datetime.now().strftime('%Y-%m-%d')}*\n"
        f"> {len(df)} setups detected | "
        f"Vol ≥3x + Strong close + Above 50d SMA + Near 52wk high"
    )
    router._post_message(channel_id, header)
    router._post_message(router.firehose_id, header)

    # Individual alerts (top 30)
    for _, row in df.head(30).iterrows():
        msg = format_bullsnort_alert(row.to_dict())
        router._post_message(channel_id, msg)
        router._post_message(router.firehose_id, msg)

    if len(df) > 30:
        overflow = f"> +{len(df) - 30} more setups — see CSV for full list"
        router._post_message(channel_id, overflow)

    print(f"  ✓ Sent {min(len(df), 30)} Bull Snort alerts to #bullsnort")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bull Snort Screener")
    parser.add_argument("--min-vol-multiple", type=float, default=3.0,
                        help="Min volume vs 50-day avg (default: 3x)")
    parser.add_argument("--min-pos-in-range", type=float, default=0.65,
                        help="Min close position in range (default: 0.65)")
    parser.add_argument("--min-price", type=float, default=20.0,
                        help="Min stock price (default: $20)")
    parser.add_argument("--min-avg-vol", type=float, default=500_000,
                        help="Min 20-day avg volume (default: 500K)")
    parser.add_argument("--max-pct-from-high", type=float, default=0.80,
                        help="Min price as fraction of 52-week high (default: 0.80)")
    parser.add_argument("--lookback", type=int, default=1,
                        help="Recent trading days to check (default: 1)")
    parser.add_argument("--indexes", nargs="+",
                        default=["sp500", "nasdaq100", "dow30", "russell2000"],
                        help="Indexes to include (default: all)")
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
    df = run_bullsnort_screen(
        min_vol_multiple=args.min_vol_multiple,
        min_pos_in_range=args.min_pos_in_range,
        min_price=args.min_price,
        min_avg_vol=args.min_avg_vol,
        max_pct_from_high=args.max_pct_from_high,
        lookback=args.lookback,
        indexes=args.indexes,
    )

    if df.empty:
        return

    # Console output
    display_cols = [
        "ticker", "date", "close", "vol_multiple", "pos_in_range",
        "day_change_pct", "pct_from_high", "snort_score",
    ]
    avail = [c for c in display_cols if c in df.columns]
    print(df[avail].head(args.top).to_string(index=False))

    # Save CSV
    outpath = args.output or f"bullsnort_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(outpath, index=False)
    print(f"\n✓ Saved {outpath} ({len(df)} rows)")

    # Slack
    if args.slack:
        print("\n📤 Sending to Slack...")
        send_to_slack(df, args.config, args.bot_token)


if __name__ == "__main__":
    main()
