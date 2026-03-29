#!/usr/bin/env python3
"""
Episodic Pivot (EP) Screener

Detects catalyst-driven institutional-quality breakouts:
  - Gap up ≥ 8% from prior close
  - Intraday follow-through ≥ 3% (close > open)
  - Relative volume ≥ 3x 20-day average
  - Price ≥ $5

Universe: S&P 500 + Nasdaq 100 + Dow 30 + Russell 2000 (deduplicated)

Usage:
  python episodic_pivot_screener.py
  python episodic_pivot_screener.py --min-gap 10 --min-rvol 4
  python episodic_pivot_screener.py --slack --config channel_config.json
  python episodic_pivot_screener.py --lookback 5  # check last 5 trading days

Route key emitted: __episodic_pivot__ → #episodic-pivots
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
    # Inline fallback if growth_screener isn't in the path
    def load_universe(indexes=None, cache_path="universe_cache.json"):
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        tickers = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"  Fallback universe: {len(tickers)} S&P 500 tickers")
        return tickers


# ---------------------------------------------------------------------------
# ATR helper
# ---------------------------------------------------------------------------

def atr_pct(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Average True Range as percentage of close."""
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift(1)).abs()
    lc = (df["Low"] - df["Close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return (atr / df["Close"]) * 100.0


# ---------------------------------------------------------------------------
# Single-ticker EP detection
# ---------------------------------------------------------------------------

def detect_episodic_pivots(
    ticker: str,
    min_gap: float = 8.0,
    min_follow_through: float = 3.0,
    min_rvol: float = 3.0,
    min_price: float = 5.0,
    lookback: int = 1,
) -> list[dict]:
    """
    Detect episodic pivot events for a single ticker.

    Args:
        ticker: stock symbol
        min_gap: minimum gap-up % from prior close (default 8%)
        min_follow_through: minimum intraday move % close vs open (default 3%)
        min_rvol: minimum relative volume vs 20-day avg (default 3x)
        min_price: minimum stock price (default $5)
        lookback: number of recent trading days to check (default 1 = today only)

    Returns:
        List of EP event dicts (empty if no events detected)
    """
    try:
        data = yf.download(ticker, period="6mo", interval="1d", auto_adjust=False, progress=False)

        # Handle multi-level columns from yfinance
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        if data.empty or len(data) < 40:
            return []

        data.dropna(inplace=True)

        # Compute metrics
        data["PrevClose"] = data["Close"].shift(1)
        data["GapPct"] = (data["Open"] - data["PrevClose"]) / data["PrevClose"] * 100.0
        data["DayMovePct"] = (data["Close"] - data["Open"]) / data["Open"] * 100.0
        data["Vol20"] = data["Volume"].rolling(20).mean()
        data["RelVol"] = data["Volume"] / data["Vol20"]
        data["ATRpct20"] = atr_pct(data, 20)
        data["High50"] = data["High"].rolling(50).max()
        data["SMA50"] = data["Close"].rolling(50).mean()

        # Check for near 52-week / 50-day high context
        data["Near50High"] = data["Close"] >= (data["High50"] * 0.95)

        events = []

        # Check last N trading days
        check_rows = data.tail(lookback)

        for idx, row in check_rows.iterrows():
            close = row["Close"]

            # Price filter
            if close < min_price:
                continue

            # EP conditions
            if row["GapPct"] < min_gap:
                continue
            if row["DayMovePct"] < min_follow_through:
                continue
            if pd.isna(row["RelVol"]) or row["RelVol"] < min_rvol:
                continue

            # Gap range (high - open as % of open)
            gap_range_pct = (row["High"] - row["Open"]) / row["Open"] * 100.0

            # Compute total move (close vs prev close)
            total_move_pct = (close - row["PrevClose"]) / row["PrevClose"] * 100.0

            # Close in upper portion of day's range (bullish confirmation)
            day_range = row["High"] - row["Low"]
            if day_range > 0:
                close_position = (close - row["Low"]) / day_range * 100.0
            else:
                close_position = 50.0

            events.append({
                "ticker": ticker,
                "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx),
                "close": round(close, 2),
                "prev_close": round(row["PrevClose"], 2),
                "open": round(row["Open"], 2),
                "high": round(row["High"], 2),
                "low": round(row["Low"], 2),
                "gap_pct": round(row["GapPct"], 1),
                "day_move_pct": round(row["DayMovePct"], 1),
                "total_move_pct": round(total_move_pct, 1),
                "rel_vol": round(row["RelVol"], 1),
                "volume": int(row["Volume"]),
                "vol_20_avg": int(row["Vol20"]) if not pd.isna(row["Vol20"]) else 0,
                "atr_pct": round(row["ATRpct20"], 2) if not pd.isna(row["ATRpct20"]) else None,
                "near_50_high": bool(row["Near50High"]) if not pd.isna(row["Near50High"]) else False,
                "close_position_pct": round(close_position, 1),
                "event_label": "__episodic_pivot__",
            })

        return events

    except Exception:
        return []


# ---------------------------------------------------------------------------
# Full screener
# ---------------------------------------------------------------------------

def run_episodic_pivot_screen(
    min_gap: float = 8.0,
    min_follow_through: float = 3.0,
    min_rvol: float = 3.0,
    min_price: float = 5.0,
    lookback: int = 1,
    indexes: list[str] | None = None,
    batch_size: int = 50,
) -> pd.DataFrame:
    """
    Run the episodic pivot screener across the full universe.

    Returns DataFrame of EP events sorted by relative volume.
    """
    print("=" * 60)
    print("EPISODIC PIVOT SCREENER")
    print(f"  Min gap: {min_gap}%")
    print(f"  Min follow-through: {min_follow_through}%")
    print(f"  Min relative volume: {min_rvol}x")
    print(f"  Min price: ${min_price}")
    print(f"  Lookback: {lookback} trading day(s)")
    print("=" * 60)

    # Load universe
    print("\n📡 Loading universe...")
    tickers = load_universe(indexes)

    # Screen
    all_events = []
    errors = 0

    print(f"🔍 Scanning {len(tickers)} tickers for episodic pivots...\n")

    for i, t in enumerate(tickers):
        if (i + 1) % batch_size == 0:
            print(f"  Progress: {i + 1}/{len(tickers)} "
                  f"({len(all_events)} EPs found, {errors} errors)")

        events = detect_episodic_pivots(
            t,
            min_gap=min_gap,
            min_follow_through=min_follow_through,
            min_rvol=min_rvol,
            min_price=min_price,
            lookback=lookback,
        )

        if events:
            all_events.extend(events)
        elif events is None:
            errors += 1

    df = pd.DataFrame(all_events)

    if df.empty:
        print(f"\n⚠ No episodic pivots detected in the last {lookback} day(s).")
        return df

    # Sort by relative volume (strongest institutional participation first)
    df = df.sort_values("rel_vol", ascending=False).reset_index(drop=True)

    # Score: gap + follow-through + rvol (normalized)
    df["ep_score"] = (
        (df["gap_pct"] / 8.0).clip(upper=3.0) * 3  # gap contribution (max 9)
        + (df["rel_vol"] / 3.0).clip(upper=3.0) * 3  # volume contribution (max 9)
        + (df["close_position_pct"] / 50.0).clip(upper=2.0)  # close position (max 2)
    ).round(1)

    print(f"\n{'=' * 60}")
    print(f"RESULTS — {len(df)} episodic pivots detected")
    print(f"  Scanned: {len(tickers)} | Found: {len(df)} | Errors: {errors}")
    print(f"{'=' * 60}\n")

    return df


# ---------------------------------------------------------------------------
# Slack integration
# ---------------------------------------------------------------------------

def format_ep_alert(row: dict) -> str:
    """Format a single EP event as a Slack message."""
    near_high = " 🔝 Near 50d high" if row.get("near_50_high") else ""
    return (
        f"🔥 *{row['ticker']}* — Episodic Pivot{near_high}\n"
        f"> Gap: +{row['gap_pct']:.1f}% | "
        f"Follow-through: +{row['day_move_pct']:.1f}% | "
        f"Total: +{row['total_move_pct']:.1f}%\n"
        f"> RelVol: {row['rel_vol']:.1f}x | "
        f"Close: ${row['close']:.2f} | "
        f"Close position: {row['close_position_pct']:.0f}%"
    )


def send_to_slack(df: pd.DataFrame, config_path: str, bot_token: str | None = None):
    """Send EP results to Slack via router."""
    try:
        from slack_router import SlackRouter
    except ImportError:
        print("  ⚠ slack_router.py not found — skipping Slack delivery")
        return

    router = SlackRouter.from_config(config_path, bot_token)
    route_key = "__episodic_pivot__"

    channel_id = router._resolve_channel(route_key)
    if not channel_id:
        print(f"  ⚠ No channel configured for {route_key}")
        return

    # Header
    header = (
        f"🔥 *EPISODIC PIVOT SCAN — {datetime.now().strftime('%Y-%m-%d')}*\n"
        f"> {len(df)} episodic pivots detected | "
        f"Gap ≥8% + Follow-through ≥3% + RelVol ≥3x"
    )
    router._post_message(channel_id, header)
    router._post_message(router.firehose_id, header)

    # Individual alerts
    for _, row in df.iterrows():
        msg = format_ep_alert(row.to_dict())
        router._post_message(channel_id, msg)
        router._post_message(router.firehose_id, msg)

    print(f"  ✓ Sent {len(df)} EP alerts to #episodic-pivots")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Episodic Pivot Screener")
    parser.add_argument("--min-gap", type=float, default=8.0,
                        help="Minimum gap-up %% (default: 8)")
    parser.add_argument("--min-follow-through", type=float, default=3.0,
                        help="Minimum intraday follow-through %% (default: 3)")
    parser.add_argument("--min-rvol", type=float, default=3.0,
                        help="Minimum relative volume multiplier (default: 3)")
    parser.add_argument("--min-price", type=float, default=5.0,
                        help="Minimum stock price (default: $5)")
    parser.add_argument("--lookback", type=int, default=1,
                        help="Number of recent trading days to check (default: 1)")
    parser.add_argument("--indexes", nargs="+",
                        default=["sp500", "nasdaq100", "dow30", "russell2000"],
                        help="Indexes to include (default: all)")
    parser.add_argument("--output", default=None,
                        help="Output CSV path")
    parser.add_argument("--slack", action="store_true",
                        help="Send results to Slack via router")
    parser.add_argument("--config", default="channel_config.json",
                        help="Channel config path for Slack routing")
    parser.add_argument("--bot-token", default=None,
                        help="Slack bot token (or set SLACK_BOT_TOKEN)")
    parser.add_argument("--top", type=int, default=30,
                        help="Print top N results to console (default: 30)")
    args = parser.parse_args()

    # Run screen
    df = run_episodic_pivot_screen(
        min_gap=args.min_gap,
        min_follow_through=args.min_follow_through,
        min_rvol=args.min_rvol,
        min_price=args.min_price,
        lookback=args.lookback,
        indexes=args.indexes,
    )

    if df.empty:
        return

    # Console output
    display_cols = [
        "ticker", "date", "close", "gap_pct", "day_move_pct",
        "total_move_pct", "rel_vol", "close_position_pct", "near_50_high", "ep_score",
    ]
    avail = [c for c in display_cols if c in df.columns]
    print(df[avail].head(args.top).to_string(index=False))

    # Save CSV
    outpath = args.output or f"episodic_pivots_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(outpath, index=False)
    print(f"\n✓ Saved {outpath} ({len(df)} rows)")

    # Slack
    if args.slack:
        print("\n📤 Sending to Slack...")
        send_to_slack(df, args.config, args.bot_token)


if __name__ == "__main__":
    main()
