#!/usr/bin/env python3
"""
Unusual Volume Screener

Detects volume surges with bullish confirmation:
  - Volume ≥ 3x 20-day average
  - Up close (close > previous close)
  - Price above 200-day SMA
  - Price ≥ $5, avg 20-day volume ≥ 300K

Universe: S&P 500 + Nasdaq 100 + Dow 30 + Russell 2000 (deduplicated)

Usage:
  python volume_surge_screener.py
  python volume_surge_screener.py --min-vol-multiple 5 --min-price 20
  python volume_surge_screener.py --slack --config channel_config.json
  python volume_surge_screener.py --lookback 5

Route key emitted: __volume_surge__ → #volume-surges
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
# Single-ticker unusual volume detection
# ---------------------------------------------------------------------------

def detect_volume_surge(
    ticker: str,
    min_vol_multiple: float = 3.0,
    min_price: float = 5.0,
    min_avg_vol: float = 300_000,
    lookback: int = 1,
) -> list[dict]:
    """
    Detect unusual volume events for a single ticker.

    Args:
        ticker: stock symbol
        min_vol_multiple: minimum volume vs 20-day avg (default 3x)
        min_price: minimum stock price (default $5)
        min_avg_vol: minimum 20-day avg volume (default 300K)
        lookback: number of recent trading days to check (default 1)

    Returns:
        List of volume surge event dicts (empty if no events)
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
        data["Vol20"] = data["Volume"].rolling(20).mean()
        data["SMA200"] = data["Close"].rolling(200).mean()
        data["SMA50"] = data["Close"].rolling(50).mean()
        data["High252"] = data["High"].rolling(252).max()

        # Range position for context
        data["Range"] = data["High"] - data["Low"]
        data["PosInRange"] = (data["Close"] - data["Low"]) / data["Range"].replace(0, np.nan)

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

            # Unusual volume: >= Nx 20-day average
            if pd.isna(bar["Vol20"]) or bar["Volume"] < min_vol_multiple * bar["Vol20"]:
                continue

            vol_multiple = bar["Volume"] / bar["Vol20"]

            # Up close
            if pd.isna(bar["PrevClose"]) or price <= float(bar["PrevClose"]):
                continue

            # Trend filter: above 200-day SMA
            if pd.isna(bar["SMA200"]) or price < bar["SMA200"]:
                continue

            # Compute context metrics
            pct_change = (price / float(bar["PrevClose"]) - 1) * 100.0
            dollar_vol = price * bar["Volume"]

            # Above 50-day SMA?
            above_sma50 = bool(not pd.isna(bar["SMA50"]) and price >= bar["SMA50"])

            # Distance from 52-week high
            pct_from_high = (price / bar["High252"] * 100.0) if not pd.isna(bar["High252"]) else None

            # Close position in range
            pos_in_range = float(bar["PosInRange"]) if not pd.isna(bar["PosInRange"]) else None

            events.append({
                "ticker": ticker,
                "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx),
                "close": round(price, 2),
                "volume": int(bar["Volume"]),
                "vol_20_avg": int(bar["Vol20"]),
                "vol_multiple": round(vol_multiple, 1),
                "pct_change": round(pct_change, 2),
                "dollar_vol": round(dollar_vol, 0),
                "sma_200": round(bar["SMA200"], 2),
                "above_sma50": above_sma50,
                "pct_from_high": round(pct_from_high, 1) if pct_from_high else None,
                "pos_in_range": round(pos_in_range, 2) if pos_in_range else None,
                "event_label": "__volume_surge__",
            })

        return events

    except Exception:
        return []


# ---------------------------------------------------------------------------
# Full screener
# ---------------------------------------------------------------------------

def run_volume_surge_screen(
    min_vol_multiple: float = 3.0,
    min_price: float = 5.0,
    min_avg_vol: float = 300_000,
    lookback: int = 1,
    indexes: list[str] | None = None,
    batch_size: int = 50,
) -> pd.DataFrame:
    """
    Run the unusual volume screener across the full universe.

    Returns DataFrame of volume surge events sorted by volume multiple.
    """
    print("=" * 60)
    print("UNUSUAL VOLUME SCREENER")
    print(f"  Min volume multiple: {min_vol_multiple}x (vs 20-day avg)")
    print(f"  Min price: ${min_price}")
    print(f"  Min avg volume: {min_avg_vol:,.0f}")
    print(f"  Trend filter: above 200-day SMA")
    print(f"  Lookback: {lookback} trading day(s)")
    print("=" * 60)

    # Load universe
    print("\n📡 Loading universe...")
    tickers = load_universe(indexes)

    # Screen
    all_events = []
    errors = 0

    print(f"🔍 Scanning {len(tickers)} tickers for unusual volume...\n")

    for i, t in enumerate(tickers):
        if (i + 1) % batch_size == 0:
            print(f"  Progress: {i + 1}/{len(tickers)} "
                  f"({len(all_events)} hits, {errors} errors)")

        events = detect_volume_surge(
            t,
            min_vol_multiple=min_vol_multiple,
            min_price=min_price,
            min_avg_vol=min_avg_vol,
            lookback=lookback,
        )

        if events:
            all_events.extend(events)
        elif events is None:
            errors += 1

    df = pd.DataFrame(all_events)

    if df.empty:
        print(f"\n⚠ No unusual volume events in the last {lookback} day(s).")
        return df

    # Sort by volume multiple
    df = df.sort_values("vol_multiple", ascending=False).reset_index(drop=True)

    # Composite score
    df["surge_score"] = (
        (df["vol_multiple"] / 3.0).clip(upper=4.0) * 3          # volume weight (max 12)
        + (df["pct_change"] / 3.0).clip(upper=2.0) * 2          # move size (max 4)
        + df["above_sma50"].astype(float) * 2                    # trend bonus
        + df["pos_in_range"].fillna(0.5).clip(upper=1.0) * 2    # close quality (max 2)
    ).round(1)

    df = df.sort_values("surge_score", ascending=False).reset_index(drop=True)

    print(f"\n{'=' * 60}")
    print(f"RESULTS — {len(df)} unusual volume events detected")
    print(f"  Scanned: {len(tickers)} | Found: {len(df)} | Errors: {errors}")
    print(f"{'=' * 60}\n")

    return df


# ---------------------------------------------------------------------------
# Slack integration
# ---------------------------------------------------------------------------

def format_volume_alert(row: dict) -> str:
    """Format a single volume surge event as a Slack message."""
    sma50_tag = " ✅ >50d" if row.get("above_sma50") else ""
    high_tag = f" | {row['pct_from_high']:.0f}% of 52wk high" if row.get("pct_from_high") else ""
    return (
        f"📈 *{row['ticker']}* — Unusual Volume{sma50_tag}\n"
        f"> Vol: {row['vol_multiple']:.1f}x avg | "
        f"Close: ${row['close']:.2f} (+{row['pct_change']:.1f}%){high_tag}\n"
        f"> Dollar vol: ${row['dollar_vol']:,.0f}"
    )


def send_to_slack(df: pd.DataFrame, config_path: str, bot_token: str | None = None):
    """Send volume surge results to Slack via router."""
    try:
        from slack_router import SlackRouter
    except ImportError:
        print("  ⚠ slack_router.py not found — skipping Slack delivery")
        return

    router = SlackRouter.from_config(config_path, bot_token)
    route_key = "__volume_surge__"

    channel_id = router._resolve_channel(route_key)
    if not channel_id:
        print(f"  ⚠ No channel configured for {route_key}")
        return

    # Header
    header = (
        f"📈 *UNUSUAL VOLUME SCAN — {datetime.now().strftime('%Y-%m-%d')}*\n"
        f"> {len(df)} surges detected | Vol ≥3x 20d avg + Up close + Above 200d SMA"
    )
    router._post_message(channel_id, header)
    router._post_message(router.firehose_id, header)

    # Individual alerts (top 30)
    for _, row in df.head(30).iterrows():
        msg = format_volume_alert(row.to_dict())
        router._post_message(channel_id, msg)
        router._post_message(router.firehose_id, msg)

    if len(df) > 30:
        overflow = f"> +{len(df) - 30} more surges — see CSV for full list"
        router._post_message(channel_id, overflow)

    print(f"  ✓ Sent {min(len(df), 30)} volume alerts to #volume-surges")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Unusual Volume Screener")
    parser.add_argument("--min-vol-multiple", type=float, default=3.0,
                        help="Min volume vs 20-day avg (default: 3x)")
    parser.add_argument("--min-price", type=float, default=5.0,
                        help="Min stock price (default: $5)")
    parser.add_argument("--min-avg-vol", type=float, default=300_000,
                        help="Min 20-day avg volume (default: 300K)")
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
    df = run_volume_surge_screen(
        min_vol_multiple=args.min_vol_multiple,
        min_price=args.min_price,
        min_avg_vol=args.min_avg_vol,
        lookback=args.lookback,
        indexes=args.indexes,
    )

    if df.empty:
        return

    # Console output
    display_cols = [
        "ticker", "date", "close", "vol_multiple", "pct_change",
        "above_sma50", "pct_from_high", "pos_in_range", "surge_score",
    ]
    avail = [c for c in display_cols if c in df.columns]
    print(df[avail].head(args.top).to_string(index=False))

    # Save CSV
    outpath = args.output or f"volume_surges_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(outpath, index=False)
    print(f"\n✓ Saved {outpath} ({len(df)} rows)")

    # Slack
    if args.slack:
        print("\n📤 Sending to Slack...")
        send_to_slack(df, args.config, args.bot_token)


if __name__ == "__main__":
    main()
