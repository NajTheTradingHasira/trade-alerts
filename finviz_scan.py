"""
Finviz Elite Screener — Automated scan orchestrator.

Runs configured Finviz Elite screens, formats results into Slack-ready
alerts, and posts to the #finviz-screens channel. Supports multiple
scan modes (premarket, midday, postclose) with per-mode screen selection.

Usage:
    python finviz_scan.py --mode premarket
    python finviz_scan.py --mode midday
    python finviz_scan.py --mode postclose
    python finviz_scan.py --mode all
    python finviz_scan.py --screen canslim_growth   # single screen
    python finviz_scan.py --list                     # list all screens
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime, timezone
from typing import Optional

# ── Ensure project root is on path ──────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research_core.finviz_client import FinvizClient, FinvizAuthError, FinvizFetchError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("finviz_scan")

# ── Slack Config ─────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
FIREHOSE_CHANNEL_ID = "C0ANZDPD4AU"  # #all-trade-alerts

# Per-screen channel routing
CHANNEL_MAP = {
    "canslim_growth":     "C0AQJDCTM1P",  # #finviz-canslim
    "new_highs":          "C0AQZQJJ00G",  # #finviz-new-highs
    "top_gainers":        "C0AQZQJLQC8",  # #finviz-top-gainers
    "top_losers":         "C0AQ5GH43HQ",  # #finviz-top-losers
    "major_news":         "C0AQJDJPE5P",  # #finviz-major-news
    "episodic_pivots":    "C0APETM14DT",  # #episodic-pivots
    "bullsnort":          "C0APG8D3H0W",  # #bullsnort
    "relative_strength":  "C0APBUEFG1Z",  # #relative-strength
    "unusual_volume":     "C0APBUDJVNX",  # #volume-surges
}

# ── Screen Registry ──────────────────────────────────────────────────────────
# Each screen has:
#   url      — Finviz Elite screener URL
#   emoji    — Slack emoji for the alert header
#   modes    — which scan modes include this screen
#   max_show — max tickers to display in Slack (keeps messages clean)
#   columns  — which CSV columns to show in the Slack table

SCREEN_REGISTRY = {
    "canslim_growth": {
        "name": "CANSLIM Growth",
        "url": "https://elite.finviz.com/screener.ashx?v=111&f=cap_smallover,fa_eps5years_o15,fa_epsqoq_o15,fa_pc_o10,fa_peg_o1,fa_roe_o15,fa_salesqoq_o15,geo_usa,sh_avgvol_o300,sh_instown_o60,ta_sma50_sa200&ft=2&ar=60",
        "emoji": "📈",
        "modes": ["premarket", "postclose"],
        "max_show": 30,
        "columns": ["Ticker", "Company", "Sector", "Market Cap", "P/E", "EPS (ttm)", "Change", "Volume"],
    },
    "episodic_pivots": {
        "name": "Episodic Pivots",
        "url": "https://elite.finviz.com/screener.ashx?v=151&f=cap_1to,ind_stocksonly,sh_curvol_9000tox,sh_relvol_1.25to,ta_change_u,ta_changeopen_u,ta_perf_1wup,ta_perf2_4wup&ft=4&o=-marketcap&ar=60&c=0,1,2,3,4,5,6,7,8,65,61,66,60,42,43,44,45,46,52,53,54,67,64,14,49,58,57,17,18,16,22,23,32,33,41,79,68",
        "emoji": "⚡",
        "modes": ["midday", "postclose"],
        "max_show": 20,
        "columns": ["Ticker", "Company", "Market Cap", "Change", "Volume", "Rel Volume"],
    },
    "bullsnort": {
        "name": "Bull Snort",
        "url": "https://elite.finviz.com/screener.ashx?v=111&f=sh_avgvol_o500,sh_price_o20,sh_relvol_o2,ta_perf2_dup&ft=4&o=industry&ar=60",
        "emoji": "🐂",
        "modes": ["midday", "postclose"],
        "max_show": 25,
        "columns": ["Ticker", "Company", "Industry", "Change", "Volume", "Rel Volume"],
    },
    "relative_strength": {
        "name": "Relative Strength",
        "url": "https://elite.finviz.com/screener.ashx?v=111&f=sh_avgvol_o500,sh_price_o20,ta_perf2_dup&ar=60",
        "emoji": "💪",
        "modes": ["postclose"],
        "max_show": 30,
        "columns": ["Ticker", "Company", "Sector", "Perf Week", "Perf Month", "Change", "Volume"],
    },
    "new_highs": {
        "name": "52-Week New Highs",
        "url": "https://elite.finviz.com/screener.ashx?v=111&s=ta_newhigh&f=sh_avgvol_o500,sh_price_o20,ta_highlow52w_nh&ft=3&ar=60",
        "emoji": "🏔️",
        "modes": ["midday", "postclose"],
        "max_show": 30,
        "columns": ["Ticker", "Company", "Sector", "Change", "Volume", "52W High"],
    },
    "top_gainers": {
        "name": "Top Gainers",
        "url": "https://elite.finviz.com/screener.ashx?v=111&s=ta_topgainers",
        "emoji": "🚀",
        "modes": ["premarket", "midday", "postclose"],
        "max_show": 20,
        "columns": ["Ticker", "Company", "Change", "Volume", "Price"],
    },
    "top_losers": {
        "name": "Top Losers",
        "url": "https://elite.finviz.com/screener.ashx?v=111&s=ta_toplosers",
        "emoji": "🔻",
        "modes": ["premarket", "midday", "postclose"],
        "max_show": 20,
        "columns": ["Ticker", "Company", "Change", "Volume", "Price"],
    },
    "major_news": {
        "name": "Major News",
        "url": "https://elite.finviz.com/screener.ashx?v=111&s=n_majornews",
        "emoji": "📰",
        "modes": ["premarket", "midday"],
        "max_show": 25,
        "columns": ["Ticker", "Company", "Sector", "Change", "Volume"],
    },
    "unusual_volume": {
        "name": "Unusual Volume",
        "url": "https://elite.finviz.com/screener.ashx?v=111&s=ta_unusualvolume&f=sh_price_o10",
        "emoji": "🔊",
        "modes": ["midday", "postclose"],
        "max_show": 25,
        "columns": ["Ticker", "Company", "Change", "Volume", "Rel Volume", "Price"],
    },
}

# ── Mode-to-screens mapping ─────────────────────────────────────────────────
SCAN_MODES = {
    "premarket": {
        "label": "🌅 Pre-Market Scan",
        "time": "8:30 AM ET",
    },
    "midday": {
        "label": "☀️ Midday Momentum Check",
        "time": "12:00 PM ET",
    },
    "postclose": {
        "label": "🌙 Post-Close Scan",
        "time": "4:30 PM ET",
    },
}


def get_screens_for_mode(mode: str) -> dict:
    """Return screen configs that apply to a given scan mode."""
    return {
        key: cfg for key, cfg in SCREEN_REGISTRY.items()
        if mode in cfg["modes"]
    }


# ── Slack Formatting ─────────────────────────────────────────────────────────

def format_screen_block(screen_key: str, screen_cfg: dict, results: list[dict]) -> str:
    """Format a single screen's results into a Slack mrkdwn block."""
    name = screen_cfg["name"]
    emoji = screen_cfg["emoji"]
    max_show = screen_cfg["max_show"]
    desired_cols = screen_cfg["columns"]

    count = len(results)
    if count == 0:
        return f"{emoji} *{name}* — _No results_\n"

    # Determine which desired columns actually exist in the data
    available_cols = list(results[0].keys()) if results else []
    show_cols = [c for c in desired_cols if c in available_cols]

    # Always ensure Ticker is first if available
    if not show_cols:
        # Fallback: show first 6 columns
        show_cols = available_cols[:6]

    # Build table
    lines = []
    lines.append(f"{emoji} *{name}* — {count} result{'s' if count != 1 else ''}")
    lines.append("")

    # Truncate to max_show
    display = results[:max_show]

    # Format as compact ticker list with key stats
    for row in display:
        ticker = row.get("Ticker", row.get("No.", "???"))
        change = row.get("Change", "")
        price = row.get("Price", "")
        volume = row.get("Volume", "")
        company = row.get("Company", "")

        # Truncate company name
        if len(company) > 25:
            company = company[:22] + "..."

        parts = [f"`{ticker}`"]
        if company:
            parts.append(company)
        if change:
            # Add color indicator
            change_val = change.replace("%", "").strip()
            try:
                if float(change_val) > 0:
                    parts.append(f"🟢 {change}")
                else:
                    parts.append(f"🔴 {change}")
            except (ValueError, TypeError):
                parts.append(change)
        if price:
            parts.append(f"${price}")
        if volume:
            parts.append(f"Vol: {volume}")

        lines.append("  ".join(parts))

    if count > max_show:
        lines.append(f"_...and {count - max_show} more_")

    lines.append("")
    return "\n".join(lines)


def build_slack_message(mode: str, screen_results: dict[str, tuple[dict, list[dict]]]) -> str:
    """Build the full Slack message for a scan mode."""
    mode_cfg = SCAN_MODES[mode]
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")

    header = f"{mode_cfg['label']}  •  {timestamp}\n{'━' * 40}\n\n"

    blocks = []
    total_hits = 0

    for screen_key, (screen_cfg, results) in screen_results.items():
        blocks.append(format_screen_block(screen_key, screen_cfg, results))
        total_hits += len(results)

    footer = f"\n{'━' * 40}\n_Finviz Elite • {len(screen_results)} screens • {total_hits} total hits_"

    return header + "\n".join(blocks) + footer


# ── Slack Posting ────────────────────────────────────────────────────────────

def post_to_slack(message: str, channel_id: str) -> bool:
    """Post message to Slack channel. Auto-joins if needed."""
    import requests as req

    if not SLACK_BOT_TOKEN:
        logger.error("SLACK_BOT_TOKEN not set — skipping Slack post")
        return False

    if not channel_id:
        logger.error("Channel ID not set — skipping Slack post")
        return False

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    # Auto-join channel first
    try:
        req.post(
            "https://slack.com/api/conversations.join",
            headers=headers,
            json={"channel": channel_id},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"conversations.join failed: {e}")

    # Split message if too long (Slack limit: 4000 chars per message)
    messages = _split_message(message, max_len=3900)

    success = True
    for msg in messages:
        try:
            resp = req.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json={"channel": channel_id, "text": msg, "unfurl_links": False},
                timeout=15,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error(f"Slack API error: {data.get('error', 'unknown')}")
                success = False
        except Exception as e:
            logger.error(f"Slack post failed: {e}")
            success = False

    return success


def _split_message(text: str, max_len: int = 3900) -> list[str]:
    """Split a long message at section boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line

    if current:
        chunks.append(current)

    return chunks


# ── CSV Export ───────────────────────────────────────────────────────────────

def save_results_csv(screen_key: str, results: list[dict], output_dir: str = "finviz_output"):
    """Save screen results to CSV for archival."""
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    filepath = os.path.join(output_dir, f"{screen_key}_{date_str}.csv")

    if not results:
        return None

    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    logger.info(f"Saved {len(results)} rows to {filepath}")
    return filepath


# ── Main Orchestrator ────────────────────────────────────────────────────────

def run_scan(mode: str = "all", single_screen: Optional[str] = None, dry_run: bool = False):
    """
    Run the Finviz scan.

    Args:
        mode: Scan mode — 'premarket', 'midday', 'postclose', or 'all'
        single_screen: If set, run only this specific screen
        dry_run: If True, print results but don't post to Slack
    """
    logger.info(f"═══ Finviz Scan Starting — mode={mode} ═══")

    # Determine which screens to run
    if single_screen:
        if single_screen not in SCREEN_REGISTRY:
            logger.error(f"Unknown screen: {single_screen}")
            logger.info(f"Available: {', '.join(SCREEN_REGISTRY.keys())}")
            return
        screens_to_run = {single_screen: SCREEN_REGISTRY[single_screen]}
    elif mode == "all":
        screens_to_run = SCREEN_REGISTRY.copy()
    else:
        screens_to_run = get_screens_for_mode(mode)

    if not screens_to_run:
        logger.warning(f"No screens configured for mode '{mode}'")
        return

    logger.info(f"Running {len(screens_to_run)} screens: {', '.join(screens_to_run.keys())}")

    # Authenticate and fetch
    screen_results = {}

    try:
        with FinvizClient() as client:
            for key, cfg in screens_to_run.items():
                logger.info(f"── Fetching: {cfg['name']} ──")
                try:
                    results = client.fetch_screen(cfg["url"])
                    screen_results[key] = (cfg, results)
                    logger.info(f"   → {len(results)} results")

                    # Save CSV
                    save_results_csv(key, results)

                except Exception as e:
                    logger.error(f"   ✗ Failed: {e}")
                    screen_results[key] = (cfg, [])

    except FinvizAuthError as e:
        logger.error(f"Authentication failed: {e}")
        # Post auth failure alert to firehose
        if not dry_run and SLACK_BOT_TOKEN and FIREHOSE_CHANNEL_ID:
            post_to_slack(
                f"🚨 *Finviz Scan Auth Failure*\n\n"
                f"Could not log into Finviz Elite. Check `FINVIZ_EMAIL` and `FINVIZ_PASSWORD` secrets.\n"
                f"Error: `{e}`",
                FIREHOSE_CHANNEL_ID,
            )
        return

    # Format and post — each screen goes to its own channel + firehose
    effective_mode = mode if mode != "all" else "postclose"

    if dry_run:
        message = build_slack_message(effective_mode, screen_results)
        print("\n" + "=" * 60)
        print("DRY RUN — Slack message preview:")
        print("=" * 60)
        print(message)
        print("=" * 60)
    else:
        for screen_key, (screen_cfg, results) in screen_results.items():
            if not results:
                continue

            block = format_screen_block(screen_key, screen_cfg, results)
            mode_cfg = SCAN_MODES[effective_mode]
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            msg = f"{mode_cfg['label']}  •  {now_str}\n\n{block}"

            # Post to dedicated channel
            channel_id = CHANNEL_MAP.get(screen_key)
            if channel_id:
                success = post_to_slack(msg, channel_id)
                if success:
                    logger.info(f"✅ Posted {screen_key} to #{screen_key} channel")
                else:
                    logger.error(f"Failed to post {screen_key}")

            # Cross-post to firehose
            if FIREHOSE_CHANNEL_ID:
                post_to_slack(msg, FIREHOSE_CHANNEL_ID)

    # Summary
    total = sum(len(r) for _, r in screen_results.values())
    logger.info(f"═══ Finviz Scan Complete — {total} total hits across {len(screen_results)} screens ═══")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="APEX Finviz Elite Screener")
    parser.add_argument(
        "--mode",
        choices=["premarket", "midday", "postclose", "all"],
        default="all",
        help="Scan mode (determines which screens run)",
    )
    parser.add_argument(
        "--screen",
        type=str,
        default=None,
        help="Run a single specific screen by key",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results to stdout instead of posting to Slack",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_screens",
        help="List all available screens and exit",
    )

    args = parser.parse_args()

    if args.list_screens:
        print("\nAvailable Finviz Screens:")
        print("─" * 50)
        for key, cfg in SCREEN_REGISTRY.items():
            modes = ", ".join(cfg["modes"])
            print(f"  {key:25s} {cfg['emoji']} {cfg['name']:25s} [{modes}]")
        print()
        return

    run_scan(mode=args.mode, single_screen=args.screen, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
