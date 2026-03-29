#!/usr/bin/env python3
"""
Pre-Market & Post-Market Movers Screener

Detects winners and losers in extended hours trading:
  - Winners: pre/post market % change ≥ +3%
  - Losers: pre/post market % change ≤ -3%
  - Pre/post market volume ≥ 50,000

Data source: yfinance (preMarketPrice / postMarketPrice from .info,
             plus extended hours bars via prepost=True)

Universe: S&P 500 + Nasdaq 100 + Dow 30 + Russell 2000 (deduplicated)

Usage:
  python extended_hours_screener.py --mode premarket
  python extended_hours_screener.py --mode postmarket
  python extended_hours_screener.py --mode premarket --min-change 5
  python extended_hours_screener.py --mode premarket --slack --config channel_config.json

Route keys emitted:
  __premarket_winner__  / __premarket_loser__  → #premarket-movers
  __postmarket_winner__ / __postmarket_loser__ → #postmarket-movers
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
# Single-ticker extended hours data
# ---------------------------------------------------------------------------

def get_extended_hours_quote(ticker: str, mode: str = "premarket") -> dict | None:
    """
    Fetch pre-market or post-market quote for a single ticker.

    Uses yfinance .info for preMarketPrice/postMarketPrice and
    regularMarketPreviousClose as the reference.

    Args:
        ticker: stock symbol
        mode: "premarket" or "postmarket"

    Returns:
        Dict with keys: ticker, prev_close, ext_price, ext_change_pct,
        ext_volume, regular_close, market_cap
        or None if data unavailable.
    """
    try:
        stk = yf.Ticker(ticker)
        info = stk.info

        if not info:
            return None

        # Previous close (reference price)
        prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
        regular_close = info.get("regularMarketPrice") or info.get("currentPrice")
        market_cap = info.get("marketCap")

        if not prev_close or prev_close <= 0:
            return None

        if mode == "premarket":
            ext_price = info.get("preMarketPrice")
            ext_change_pct = info.get("preMarketChangePercent")
            ext_volume = info.get("preMarketVolume", 0) or 0

            # If preMarketChangePercent is available as a decimal (0.05 = 5%)
            if ext_change_pct and abs(ext_change_pct) < 1:
                ext_change_pct = ext_change_pct * 100.0

        elif mode == "postmarket":
            ext_price = info.get("postMarketPrice")
            ext_change_pct = info.get("postMarketChangePercent")
            ext_volume = info.get("postMarketVolume", 0) or 0

            # Reference for post-market is the regular close, not prev close
            if regular_close and regular_close > 0:
                prev_close = regular_close

            if ext_change_pct and abs(ext_change_pct) < 1:
                ext_change_pct = ext_change_pct * 100.0
        else:
            return None

        if ext_price is None or ext_price <= 0:
            return None

        # Calculate pct change if not provided
        if ext_change_pct is None:
            ext_change_pct = (ext_price / prev_close - 1) * 100.0

        # Get avg volume for context
        avg_vol = info.get("averageDailyVolume10Day") or info.get("averageVolume") or 0

        return {
            "ticker": ticker,
            "prev_close": round(prev_close, 2),
            "ext_price": round(ext_price, 2),
            "ext_change_pct": round(ext_change_pct, 2),
            "ext_volume": int(ext_volume),
            "regular_close": round(regular_close, 2) if regular_close else None,
            "avg_daily_vol": int(avg_vol),
            "market_cap": int(market_cap) if market_cap else None,
            "dollar_ext_vol": round(ext_price * ext_volume, 0) if ext_volume else 0,
        }

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Full screener
# ---------------------------------------------------------------------------

def run_extended_hours_screen(
    mode: str = "premarket",
    min_change_pct: float = 3.0,
    min_ext_volume: int = 50_000,
    min_price: float = 1.0,
    indexes: list[str] | None = None,
    batch_size: int = 50,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run the pre-market or post-market movers screener.

    Args:
        mode: "premarket" or "postmarket"
        min_change_pct: minimum absolute % change to qualify (default 3%)
        min_ext_volume: minimum extended hours volume (default 50K)
        min_price: minimum stock price (default $1)
        indexes: which index universes to include
        batch_size: progress reporting interval

    Returns:
        Tuple of (winners DataFrame, losers DataFrame)
    """
    label = "PRE-MARKET" if mode == "premarket" else "POST-MARKET"

    print("=" * 60)
    print(f"{label} MOVERS SCREENER")
    print(f"  Min move: ±{min_change_pct}%")
    print(f"  Min extended hours volume: {min_ext_volume:,}")
    print(f"  Min price: ${min_price}")
    print("=" * 60)

    # Load universe
    print("\n📡 Loading universe...")
    tickers = load_universe(indexes)

    # Screen
    all_quotes = []
    no_data = 0
    errors = 0

    print(f"🔍 Fetching {label.lower()} quotes for {len(tickers)} tickers...\n")

    for i, t in enumerate(tickers):
        if (i + 1) % batch_size == 0:
            print(f"  Progress: {i + 1}/{len(tickers)} "
                  f"({len(all_quotes)} with data, {no_data} no ext data)")

        quote = get_extended_hours_quote(t, mode)

        if quote is None:
            no_data += 1
            continue

        # Price filter
        if quote["ext_price"] < min_price:
            continue

        # Volume filter
        if quote["ext_volume"] < min_ext_volume:
            continue

        all_quotes.append(quote)

    df = pd.DataFrame(all_quotes)

    if df.empty:
        print(f"\n⚠ No {label.lower()} data available.")
        return pd.DataFrame(), pd.DataFrame()

    # Split into winners and losers
    winners = df[df["ext_change_pct"] >= min_change_pct].copy()
    losers = df[df["ext_change_pct"] <= -min_change_pct].copy()

    # Sort
    winners = winners.sort_values("ext_change_pct", ascending=False).reset_index(drop=True)
    losers = losers.sort_values("ext_change_pct", ascending=True).reset_index(drop=True)

    # Add event labels
    if not winners.empty:
        route = f"__{mode}_winner__"
        winners["event_label"] = route

    if not losers.empty:
        route = f"__{mode}_loser__"
        losers["event_label"] = route

    print(f"\n{'=' * 60}")
    print(f"{label} MOVERS RESULTS")
    print(f"  Total with ext data: {len(df)} | "
          f"Winners (≥+{min_change_pct}%): {len(winners)} | "
          f"Losers (≤-{min_change_pct}%): {len(losers)}")
    print(f"  No ext data: {no_data}")
    print(f"{'=' * 60}\n")

    return winners, losers


# ---------------------------------------------------------------------------
# Slack integration
# ---------------------------------------------------------------------------

def format_mover_alert(row: dict, direction: str, mode: str) -> str:
    """Format a single mover event as a Slack message."""
    label = "Pre-market" if mode == "premarket" else "Post-market"
    emoji = "🟢" if direction == "winner" else "🔴"
    sign = "+" if row["ext_change_pct"] > 0 else ""

    vol_str = f"{row['ext_volume']:,}" if row.get("ext_volume") else "N/A"

    return (
        f"{emoji} *{row['ticker']}* — {label} {direction.title()}\n"
        f"> {sign}{row['ext_change_pct']:.2f}% | "
        f"Ext price: ${row['ext_price']:.2f} (prev: ${row['prev_close']:.2f})\n"
        f"> Ext vol: {vol_str}"
    )


def send_to_slack(
    winners: pd.DataFrame,
    losers: pd.DataFrame,
    mode: str,
    config_path: str,
    bot_token: str | None = None,
):
    """Send movers results to Slack via router."""
    try:
        from slack_router import SlackRouter
    except ImportError:
        print("  ⚠ slack_router.py not found — skipping Slack delivery")
        return

    router = SlackRouter.from_config(config_path, bot_token)
    label = "PRE-MARKET" if mode == "premarket" else "POST-MARKET"

    # Determine channel
    winner_key = f"__{mode}_winner__"
    loser_key = f"__{mode}_loser__"

    # Both winner and loser keys should resolve to same channel
    channel_id = router._resolve_channel(winner_key)
    if not channel_id:
        # Try the other key
        channel_id = router._resolve_channel(loser_key)
    if not channel_id:
        print(f"  ⚠ No channel configured for {mode} movers")
        return

    # Header
    header = (
        f"{'🌅' if mode == 'premarket' else '🌙'} "
        f"*{label} MOVERS — {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n"
        f"> Winners: {len(winners)} | Losers: {len(losers)}"
    )
    router._post_message(channel_id, header)
    router._post_message(router.firehose_id, header)

    # Winners (top 20)
    if not winners.empty:
        router._post_message(channel_id, f"*— {label} WINNERS —*")
        for _, row in winners.head(20).iterrows():
            msg = format_mover_alert(row.to_dict(), "winner", mode)
            router._post_message(channel_id, msg)
            router._post_message(router.firehose_id, msg)

        if len(winners) > 20:
            router._post_message(channel_id, f"> +{len(winners) - 20} more winners")

    # Losers (top 20)
    if not losers.empty:
        router._post_message(channel_id, f"*— {label} LOSERS —*")
        for _, row in losers.head(20).iterrows():
            msg = format_mover_alert(row.to_dict(), "loser", mode)
            router._post_message(channel_id, msg)
            router._post_message(router.firehose_id, msg)

        if len(losers) > 20:
            router._post_message(channel_id, f"> +{len(losers) - 20} more losers")

    total = min(len(winners), 20) + min(len(losers), 20)
    channel_name = "premarket-movers" if mode == "premarket" else "postmarket-movers"
    print(f"  ✓ Sent {total} alerts to #{channel_name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extended Hours Movers Screener")
    parser.add_argument("--mode", required=True, choices=["premarket", "postmarket"],
                        help="Screening mode: premarket or postmarket")
    parser.add_argument("--min-change", type=float, default=3.0,
                        help="Min absolute %% change (default: 3)")
    parser.add_argument("--min-ext-volume", type=int, default=50_000,
                        help="Min extended hours volume (default: 50K)")
    parser.add_argument("--min-price", type=float, default=1.0,
                        help="Min stock price (default: $1)")
    parser.add_argument("--indexes", nargs="+",
                        default=["sp500", "nasdaq100", "dow30", "russell2000"],
                        help="Indexes to include (default: all)")
    parser.add_argument("--output", default=None,
                        help="Output CSV path prefix")
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
    winners, losers = run_extended_hours_screen(
        mode=args.mode,
        min_change_pct=args.min_change,
        min_ext_volume=args.min_ext_volume,
        min_price=args.min_price,
        indexes=args.indexes,
    )

    label = "premarket" if args.mode == "premarket" else "postmarket"
    date_str = datetime.now().strftime("%Y%m%d")

    # Console output
    display_cols = ["ticker", "ext_price", "ext_change_pct", "ext_volume", "prev_close"]

    if not winners.empty:
        avail = [c for c in display_cols if c in winners.columns]
        print(f"=== {label.upper()} WINNERS ===")
        print(winners[avail].head(args.top).to_string(index=False))

    if not losers.empty:
        avail = [c for c in display_cols if c in losers.columns]
        print(f"\n=== {label.upper()} LOSERS ===")
        print(losers[avail].head(args.top).to_string(index=False))

    # Save CSVs
    prefix = args.output or label
    if not winners.empty:
        w_path = f"{prefix}_winners_{date_str}.csv"
        winners.to_csv(w_path, index=False)
        print(f"\n✓ Saved {w_path} ({len(winners)} rows)")

    if not losers.empty:
        l_path = f"{prefix}_losers_{date_str}.csv"
        losers.to_csv(l_path, index=False)
        print(f"✓ Saved {l_path} ({len(losers)} rows)")

    # Slack
    if args.slack:
        print("\n📤 Sending to Slack...")
        send_to_slack(winners, losers, args.mode, args.config, args.bot_token)


if __name__ == "__main__":
    main()
