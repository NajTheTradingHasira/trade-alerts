#!/usr/bin/env python3
"""
Growth Screener — 20/20 and 40/40 modes.

Screens for stocks with minimum YoY sales growth AND EPS growth,
filtered by price and dollar volume liquidity.

Modes:
  20/20 → ≥20% sales growth + ≥20% EPS growth
  40/40 → ≥40% sales growth + ≥40% EPS growth

Universe: S&P 500 + Nasdaq 100 + Dow 30 + Russell 2000 (deduplicated)

Usage:
  python growth_screener.py --mode 2020
  python growth_screener.py --mode 4040
  python growth_screener.py --mode 2020 --slack --config channel_config.json
  python growth_screener.py --mode 4040 --min-price 10 --min-dollar-vol 10000000

Route keys emitted:
  __20_20__  → #20-20-screener
  __40_40__  → #40-40-screener
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Universe loader — pulls tickers from all major US indexes
# ---------------------------------------------------------------------------

def load_universe(indexes: list[str] | None = None, cache_path: str = "universe_cache.json") -> list[str]:
    """
    Load deduplicated ticker universe from major US indexes.

    Indexes supported: sp500, nasdaq100, dow30, russell2000
    Default: all four.

    Caches to disk for 24 hours to avoid repeated Wikipedia scrapes.
    """
    if indexes is None:
        indexes = ["sp500", "nasdaq100", "dow30", "russell2000"]

    # Check cache
    cache = Path(cache_path)
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
            cache_time = datetime.fromisoformat(cached.get("timestamp", "2000-01-01"))
            age_hours = (datetime.now() - cache_time).total_seconds() / 3600
            if age_hours < 24 and set(indexes).issubset(set(cached.get("indexes", []))):
                print(f"  Using cached universe ({len(cached['tickers'])} tickers, {age_hours:.1f}h old)")
                return cached["tickers"]
        except Exception:
            pass

    all_tickers = set()

    if "sp500" in indexes:
        try:
            print("  Fetching S&P 500 constituents...")
            tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
            sp500 = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
            all_tickers.update(sp500)
            print(f"    → {len(sp500)} tickers")
        except Exception as e:
            print(f"    ⚠ S&P 500 fetch failed: {e}")

    if "nasdaq100" in indexes:
        try:
            print("  Fetching Nasdaq 100 constituents...")
            tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
            # Find the table with a 'Ticker' or 'Symbol' column
            for t in tables:
                for col in ["Ticker", "Symbol"]:
                    if col in t.columns:
                        nq100 = t[col].str.replace(".", "-", regex=False).tolist()
                        all_tickers.update(nq100)
                        print(f"    → {len(nq100)} tickers")
                        break
                else:
                    continue
                break
        except Exception as e:
            print(f"    ⚠ Nasdaq 100 fetch failed: {e}")

    if "dow30" in indexes:
        try:
            print("  Fetching Dow 30 constituents...")
            tables = pd.read_html("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")
            for t in tables:
                for col in ["Symbol", "Ticker"]:
                    if col in t.columns:
                        dow = t[col].str.replace(".", "-", regex=False).tolist()
                        all_tickers.update(dow)
                        print(f"    → {len(dow)} tickers")
                        break
                else:
                    continue
                break
        except Exception as e:
            print(f"    ⚠ Dow 30 fetch failed: {e}")

    if "russell2000" in indexes:
        try:
            print("  Fetching Russell 2000 constituents...")
            # Russell 2000 isn't easily scraped from Wikipedia.
            # Use the iShares IWM holdings as a proxy.
            iwm = yf.Ticker("IWM")
            # Try to get holdings if available
            try:
                holdings = iwm.get_holdings()
                if holdings is not None and not holdings.empty:
                    for col in ["Symbol", "Ticker", "symbol", "ticker"]:
                        if col in holdings.columns:
                            r2k = holdings[col].dropna().tolist()
                            all_tickers.update(r2k)
                            print(f"    → {len(r2k)} tickers (from IWM holdings)")
                            break
                else:
                    raise ValueError("No holdings data")
            except Exception:
                # Fallback: use a broader Wikipedia scrape
                try:
                    tables = pd.read_html(
                        "https://en.wikipedia.org/wiki/Russell_2000_Index",
                        match="Ticker|Symbol",
                    )
                    if tables:
                        for col in ["Ticker", "Symbol"]:
                            if col in tables[0].columns:
                                r2k = tables[0][col].str.replace(".", "-", regex=False).tolist()
                                all_tickers.update(r2k)
                                print(f"    → {len(r2k)} tickers")
                                break
                except Exception:
                    print("    ⚠ Russell 2000 not available — using S&P SmallCap 600 as proxy")
                    try:
                        tables = pd.read_html(
                            "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
                        )
                        sp600 = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
                        all_tickers.update(sp600)
                        print(f"    → {len(sp600)} tickers (S&P 600 proxy)")
                    except Exception as e2:
                        print(f"    ⚠ SmallCap fallback also failed: {e2}")
        except Exception as e:
            print(f"    ⚠ Russell 2000 fetch failed: {e}")

    tickers = sorted(all_tickers)

    # Cache
    try:
        cache.write_text(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "indexes": indexes,
            "tickers": tickers,
        }))
    except Exception:
        pass

    print(f"  Total universe: {len(tickers)} unique tickers\n")
    return tickers


# ---------------------------------------------------------------------------
# Growth computation helpers
# ---------------------------------------------------------------------------

def pct_change_safe(current: float, prev: float) -> float:
    """Safe percentage change calculation."""
    if prev is None or prev == 0 or np.isnan(prev):
        return np.nan
    return (current - prev) / abs(prev) * 100.0


def get_growth_metrics(ticker: str) -> dict | None:
    """
    Fetch price, volume, and fundamental growth metrics for a single ticker.

    Returns dict with keys:
      ticker, close, avg_dollar_vol, sales_growth_pct, eps_growth_pct
    or None if data is unavailable.
    """
    try:
        stk = yf.Ticker(ticker)

        # Price & liquidity
        hist = stk.history(period="3mo")
        if hist.empty or len(hist) < 5:
            return None

        close = hist["Close"].iloc[-1]
        avg_vol = hist["Volume"].tail(20).mean()
        avg_dollar_vol = close * avg_vol

        # Financials
        fin = stk.financials
        if fin is None or fin.empty:
            return None

        # Revenue growth
        rev_row = fin.loc[fin.index.str.lower().str.contains("total revenue")]
        if rev_row.empty:
            return None

        rev = rev_row.iloc[0]
        if len(rev) < 2:
            return None

        rev_curr = float(rev.iloc[0])
        rev_prev = float(rev.iloc[1])
        sales_growth = pct_change_safe(rev_curr, rev_prev)

        # EPS growth (net income as proxy)
        net_income_row = fin.loc[fin.index.str.lower().str.contains("net income")]
        if net_income_row.empty:
            eps_growth = np.nan
        else:
            ni = net_income_row.iloc[0]
            if len(ni) < 2:
                eps_growth = np.nan
            else:
                ni_curr = float(ni.iloc[0])
                ni_prev = float(ni.iloc[1])
                eps_growth = pct_change_safe(ni_curr, ni_prev)

        return {
            "ticker": ticker,
            "close": round(close, 2),
            "avg_dollar_vol": round(avg_dollar_vol, 0),
            "sales_growth_pct": round(sales_growth, 1) if not np.isnan(sales_growth) else np.nan,
            "eps_growth_pct": round(eps_growth, 1) if not np.isnan(eps_growth) else np.nan,
        }

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------

def run_growth_screen(
    mode: str = "2020",
    min_price: float = 20.0,
    min_dollar_vol: float = 20_000_000,
    indexes: list[str] | None = None,
    batch_size: int = 50,
) -> pd.DataFrame:
    """
    Run the growth screener.

    Args:
        mode: "2020" or "4040" — sets the growth threshold
        min_price: minimum stock price filter
        min_dollar_vol: minimum average daily dollar volume
        indexes: which indexes to include in universe
        batch_size: how many tickers to process before printing progress

    Returns:
        DataFrame of passing tickers sorted by sales + EPS growth
    """
    threshold = 20.0 if mode == "2020" else 40.0
    label = f"{int(threshold)}/{int(threshold)}"

    print("=" * 60)
    print(f"GROWTH SCREENER — {label} MODE")
    print(f"  Min price: ${min_price:.0f}")
    print(f"  Min dollar volume: ${min_dollar_vol:,.0f}/day")
    print(f"  Sales growth threshold: ≥{threshold:.0f}%")
    print(f"  EPS growth threshold: ≥{threshold:.0f}%")
    print("=" * 60)

    # Load universe
    print("\n📡 Loading universe...")
    tickers = load_universe(indexes)

    # Screen
    results = []
    skipped = 0
    errors = 0

    print(f"🔍 Screening {len(tickers)} tickers...\n")

    for i, t in enumerate(tickers):
        if (i + 1) % batch_size == 0:
            print(f"  Progress: {i + 1}/{len(tickers)} "
                  f"({len(results)} passing so far, {skipped} filtered, {errors} errors)")

        metrics = get_growth_metrics(t)

        if metrics is None:
            errors += 1
            continue

        # Price filter
        if metrics["close"] < min_price:
            skipped += 1
            continue

        # Liquidity filter
        if metrics["avg_dollar_vol"] < min_dollar_vol:
            skipped += 1
            continue

        # Growth filters
        if pd.isna(metrics["sales_growth_pct"]) or metrics["sales_growth_pct"] < threshold:
            skipped += 1
            continue

        if pd.isna(metrics["eps_growth_pct"]) or metrics["eps_growth_pct"] < threshold:
            skipped += 1
            continue

        results.append(metrics)

    df = pd.DataFrame(results)

    if df.empty:
        print(f"\n⚠ No tickers passed the {label} screen.")
        return df

    # Sort by combined growth
    df["combined_growth"] = df["sales_growth_pct"] + df["eps_growth_pct"]
    df = df.sort_values("combined_growth", ascending=False).reset_index(drop=True)

    print(f"\n{'=' * 60}")
    print(f"RESULTS — {len(df)} tickers passed {label} screen")
    print(f"  Screened: {len(tickers)} | Passed: {len(df)} "
          f"| Filtered: {skipped} | Errors: {errors}")
    print(f"{'=' * 60}\n")

    return df


# ---------------------------------------------------------------------------
# Slack integration
# ---------------------------------------------------------------------------

def format_growth_alert(row: dict, mode: str) -> str:
    """Format a single screener result as a Slack message."""
    emoji = "📐"
    label = "20/20" if mode == "2020" else "40/40"
    return (
        f"{emoji} *{row['ticker']}* — {label} Screen\n"
        f"> Price: ${row['close']:.2f} | "
        f"Sales Growth: +{row['sales_growth_pct']:.1f}% | "
        f"EPS Growth: +{row['eps_growth_pct']:.1f}% | "
        f"Dollar Vol: ${row['avg_dollar_vol']:,.0f}/day"
    )


def send_to_slack(df: pd.DataFrame, mode: str, config_path: str, bot_token: str | None = None):
    """Send screener results to the appropriate Slack channel via router."""
    try:
        from slack_router import SlackRouter
    except ImportError:
        print("  ⚠ slack_router.py not found — skipping Slack delivery")
        return

    router = SlackRouter.from_config(config_path, bot_token)
    route_key = "__20_20__" if mode == "2020" else "__40_40__"
    label = "20/20" if mode == "2020" else "40/40"

    # Resolve channel
    channel_id = router._resolve_channel(route_key)
    if not channel_id:
        print(f"  ⚠ No channel configured for route key {route_key}")
        return

    # Header
    header = (
        f"📐 *{label} GROWTH SCREEN — {datetime.now().strftime('%Y-%m-%d')}*\n"
        f"> {len(df)} names passed | Min growth: {20 if mode == '2020' else 40}% sales + EPS"
    )
    router._post_message(channel_id, header)
    router._post_message(router.firehose_id, header)

    # Individual alerts (top 30)
    for _, row in df.head(30).iterrows():
        msg = format_growth_alert(row.to_dict(), mode)
        router._post_message(channel_id, msg)
        router._post_message(router.firehose_id, msg)

    if len(df) > 30:
        overflow = f"> +{len(df) - 30} more names passed — see CSV for full list"
        router._post_message(channel_id, overflow)

    print(f"  ✓ Sent {min(len(df), 30)} alerts to #{router._channel_id_by_name(route_key) or label}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Growth screener (20/20 and 40/40)")
    parser.add_argument("--mode", required=True, choices=["2020", "4040"],
                        help="Screening mode: 2020 or 4040")
    parser.add_argument("--min-price", type=float, default=20.0,
                        help="Minimum stock price (default: $20)")
    parser.add_argument("--min-dollar-vol", type=float, default=20_000_000,
                        help="Minimum avg daily dollar volume (default: $20M)")
    parser.add_argument("--indexes", nargs="+",
                        default=["sp500", "nasdaq100", "dow30", "russell2000"],
                        help="Indexes to include (default: all)")
    parser.add_argument("--output", default=None,
                        help="Output CSV path (default: growth_{mode}_{date}.csv)")
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
    df = run_growth_screen(
        mode=args.mode,
        min_price=args.min_price,
        min_dollar_vol=args.min_dollar_vol,
        indexes=args.indexes,
    )

    if df.empty:
        return

    # Console output
    print(df.head(args.top).to_string(index=False))

    # Save CSV
    outpath = args.output or f"growth_{args.mode}_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(outpath, index=False)
    print(f"\n✓ Saved {outpath} ({len(df)} rows)")

    # Slack
    if args.slack:
        print("\n📤 Sending to Slack...")
        send_to_slack(df, args.mode, args.config, args.bot_token)


if __name__ == "__main__":
    main()
