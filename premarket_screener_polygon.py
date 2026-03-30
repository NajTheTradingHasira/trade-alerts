#!/usr/bin/env python3
"""
premarket_screener_polygon.py — Polygon.io Pre-Market Movers
═════════════════════════════════════════════════════════════════
Fetches ALL US stock snapshots in a single Polygon API call (~9,000 tickers),
filters for pre-market movers, posts to Slack, and saves CSV.

Uses: GET /v2/snapshot/locale/us/markets/stocks/tickers

Route keys: premarket_winner, premarket_loser

Usage:
  python premarket_screener_polygon.py
  python premarket_screener_polygon.py --min-change 5 --min-volume 100000
  python premarket_screener_polygon.py --slack --config channel_config.json
  python premarket_screener_polygon.py --dry-run

Env vars:
  POLYGON_API_KEY  — required
  SLACK_BOT_TOKEN  — required for --slack
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency. Install with: pip install requests")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# POLYGON SNAPSHOT
# ═══════════════════════════════════════════════════════════════

POLYGON_BASE = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"


def fetch_snapshots(api_key: str) -> list[dict]:
    """
    Fetch all US stock snapshots from Polygon in one call.
    Returns list of ticker snapshot dicts.
    """
    resp = requests.get(POLYGON_BASE, params={"apiKey": api_key}, timeout=30)

    if resp.status_code == 403:
        print("  ✗ Polygon API: 403 Forbidden — check your API key and subscription tier.")
        print("    Snapshots require a paid Polygon plan (Starter+).")
        sys.exit(1)
    if resp.status_code == 429:
        print("  ✗ Polygon API: 429 Rate Limited — wait and retry.")
        sys.exit(1)

    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "OK":
        print(f"  ✗ Polygon API error: {data.get('status')} — {data.get('error', 'unknown')}")
        sys.exit(1)

    tickers = data.get("tickers", [])
    print(f"  Fetched {len(tickers)} ticker snapshots from Polygon")
    return tickers


# ═══════════════════════════════════════════════════════════════
# FILTER & CLASSIFY
# ═══════════════════════════════════════════════════════════════

def filter_movers(snapshots: list[dict], min_change: float, min_volume: int,
                  min_price: float) -> tuple[list[dict], list[dict]]:
    """
    Filter snapshots for pre-market movers.

    Returns (winners, losers) sorted by absolute change descending.
    """
    winners = []
    losers = []

    for snap in snapshots:
        ticker = snap.get("ticker", "")
        change_pct = snap.get("todaysChangePerc", 0)
        today = snap.get("day", {})
        prev = snap.get("prevDay", {})
        last_trade = snap.get("lastTrade", {})

        # Current price from last trade or today's close
        price = last_trade.get("p", 0) or today.get("c", 0) or today.get("vw", 0)
        volume = today.get("v", 0) or 0
        prev_volume = prev.get("v", 0) or 0

        # Skip if missing essential data
        if not price or price < min_price:
            continue
        if volume < min_volume:
            continue
        if not change_pct:
            continue

        # Skip OTC / non-standard tickers (contain digits or too long)
        if len(ticker) > 5 or not ticker.isalpha():
            continue

        # Relative volume
        rvol = round(volume / prev_volume, 1) if prev_volume > 0 else 0

        entry = {
            "ticker": ticker,
            "price": round(price, 2),
            "change_pct": round(change_pct, 2),
            "volume": volume,
            "prev_volume": prev_volume,
            "rvol": rvol,
            "prev_close": round(prev.get("c", 0), 2),
            "day_open": round(today.get("o", 0), 2),
            "day_high": round(today.get("h", 0), 2),
            "day_low": round(today.get("l", 0), 2),
        }

        if change_pct >= min_change:
            winners.append(entry)
        elif change_pct <= -min_change:
            losers.append(entry)

    # Sort by absolute change descending
    winners.sort(key=lambda x: x["change_pct"], reverse=True)
    losers.sort(key=lambda x: x["change_pct"])

    return winners, losers


# ═══════════════════════════════════════════════════════════════
# OUTPUT — TERMINAL
# ═══════════════════════════════════════════════════════════════

def fmt_vol(v: int) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return str(v)


def print_table(winners: list[dict], losers: list[dict]):
    """Print results as formatted terminal table."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    print(f"\n  ══ APEX PRE-MARKET MOVERS — {now} ══")
    print(f"  {len(winners)} winner(s) | {len(losers)} loser(s)\n")

    if winners:
        header = f"  {'Ticker':<7} {'Price':>8} {'Chg %':>8} {'RVol':>6} {'Volume':>10} {'PrevCl':>8}"
        print(f"  📈 WINNERS (Gap Up)")
        print(f"  {'─' * 55}")
        print(header)
        print(f"  {'─' * 55}")
        for w in winners[:30]:
            print(f"  {w['ticker']:<7} ${w['price']:>7.2f} {w['change_pct']:>+7.1f}% {w['rvol']:>5.1f}x {fmt_vol(w['volume']):>10} ${w['prev_close']:>7.2f}")
        print()

    if losers:
        header = f"  {'Ticker':<7} {'Price':>8} {'Chg %':>8} {'RVol':>6} {'Volume':>10} {'PrevCl':>8}"
        print(f"  📉 LOSERS (Gap Down)")
        print(f"  {'─' * 55}")
        print(header)
        print(f"  {'─' * 55}")
        for l in losers[:30]:
            print(f"  {l['ticker']:<7} ${l['price']:>7.2f} {l['change_pct']:>+7.1f}% {l['rvol']:>5.1f}x {fmt_vol(l['volume']):>10} ${l['prev_close']:>7.2f}")
        print()


# ═══════════════════════════════════════════════════════════════
# OUTPUT — CSV
# ═══════════════════════════════════════════════════════════════

def write_csv(winners: list[dict], losers: list[dict]) -> str:
    """Write results to timestamped CSV in output/ directory."""
    if not winners and not losers:
        return ""

    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = out_dir / f"premarket_movers_{ts}.csv"

    fields = ["direction", "ticker", "price", "change_pct", "volume", "rvol", "prev_close"]

    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for w in winners:
            writer.writerow({**w, "direction": "WINNER"})
        for l in losers:
            writer.writerow({**l, "direction": "LOSER"})

    print(f"  CSV saved: {filename}")
    return str(filename)


# ═══════════════════════════════════════════════════════════════
# OUTPUT — SLACK
# ═══════════════════════════════════════════════════════════════

def build_slack_message(winners: list[dict], losers: list[dict]) -> str:
    """Build formatted Slack message."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    lines = [
        f"*:sunrise: APEX Pre-Market Movers* — {now}",
        f"{len(winners)} winner(s) | {len(losers)} loser(s)",
        "",
    ]

    if winners:
        lines.append("*:chart_with_upwards_trend: WINNERS (Gap Up)*")
        for w in winners[:25]:
            lines.append(
                f"> *{w['ticker']}* ${w['price']:.2f} ({w['change_pct']:+.1f}%) "
                f"| RVol {w['rvol']:.1f}x | Vol {fmt_vol(w['volume'])}"
            )
        if len(winners) > 25:
            lines.append(f"> _...and {len(winners) - 25} more_")
        lines.append("")

    if losers:
        lines.append("*:chart_with_downwards_trend: LOSERS (Gap Down)*")
        for l in losers[:25]:
            lines.append(
                f"> *{l['ticker']}* ${l['price']:.2f} ({l['change_pct']:+.1f}%) "
                f"| RVol {l['rvol']:.1f}x | Vol {fmt_vol(l['volume'])}"
            )
        if len(losers) > 25:
            lines.append(f"> _...and {len(losers) - 25} more_")
        lines.append("")

    if not winners and not losers:
        lines.append("_No significant pre-market movers today._")

    return "\n".join(lines)


def send_to_slack(winners: list[dict], losers: list[dict], config_path: str):
    """Post results to Slack via slack_router."""
    try:
        from slack_router import SlackRouter
        router = SlackRouter.from_config(config_path)
    except ImportError:
        print("  ⚠ slack_router.py not found — falling back to direct post")
        _send_direct(winners, losers)
        return
    except Exception as e:
        print(f"  ⚠ SlackRouter init failed: {e} — falling back to direct post")
        _send_direct(winners, losers)
        return

    msg = build_slack_message(winners, losers)

    # Post via router — sends to routed channel + firehose
    if winners:
        router.send("premarket_winner", msg)
        print(f"  ✓ Posted winners to #premarket-movers + #firehose")
    if losers and not winners:
        # Only post losers separately if no winners message was sent
        router.send("premarket_loser", msg)
        print(f"  ✓ Posted losers to #premarket-movers + #firehose")
    if not winners and not losers:
        router.send("premarket_winner", msg)
        print(f"  ✓ Posted 'no movers' to #premarket-movers + #firehose")


def _send_direct(winners: list[dict], losers: list[dict]):
    """Fallback: post directly via requests if slack_router unavailable."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("  ✗ SLACK_BOT_TOKEN not set")
        return

    msg = build_slack_message(winners, losers)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for channel_id, name in [("C0APMTA03HS", "premarket-movers"), ("C0ANZDPD4AU", "firehose")]:
        try:
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json={"channel": channel_id, "text": msg, "mrkdwn": True},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                print(f"  ✓ Posted to #{name}")
            else:
                print(f"  ✗ Slack error (#{name}): {data.get('error')}")
        except Exception as e:
            print(f"  ✗ Failed to post to #{name}: {e}")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Polygon.io Pre-Market Movers Screener")
    parser.add_argument("--min-change", type=float, default=3.0,
                        help="Minimum absolute %% change (default: 3)")
    parser.add_argument("--min-volume", type=int, default=50_000,
                        help="Minimum volume (default: 50,000)")
    parser.add_argument("--min-price", type=float, default=5.0,
                        help="Minimum price (default: $5)")
    parser.add_argument("--slack", action="store_true",
                        help="Post results to Slack")
    parser.add_argument("--config", default="channel_config.json",
                        help="Path to channel_config.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without posting")
    args = parser.parse_args()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 60}")
    print(f"  APEX Pre-Market Screener (Polygon.io)")
    print(f"  Filters: >= {args.min_change}% change, >= {fmt_vol(args.min_volume)} vol, >= ${args.min_price:.0f}")
    print(f"  {now}")
    print(f"{'=' * 60}\n")

    # Check API key
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print("  ✗ POLYGON_API_KEY not set. Export it before running.")
        sys.exit(1)

    if args.dry_run:
        print("  [DRY RUN] Would fetch Polygon snapshots and filter for pre-market movers")
        print(f"  [DRY RUN] Slack: {'enabled' if args.slack else 'disabled'}")
        print(f"  [DRY RUN] Config: {args.config}")
        return

    # Fetch all snapshots (single API call)
    print("  Fetching Polygon snapshot (all US tickers)...")
    snapshots = fetch_snapshots(api_key)

    # Filter
    winners, losers = filter_movers(snapshots, args.min_change, args.min_volume, args.min_price)
    print(f"  Found {len(winners)} winners, {len(losers)} losers\n")

    # Terminal output
    print_table(winners, losers)

    # CSV output
    write_csv(winners, losers)

    # Slack output
    if args.slack:
        print("  Sending to Slack...")
        send_to_slack(winners, losers, args.config)

    print(f"\n  Done. {len(winners)} winners, {len(losers)} losers.\n")


if __name__ == "__main__":
    main()
