#!/usr/bin/env python3
"""
Create Finviz Slack channels and print their IDs.
Run: SLACK_BOT_TOKEN=xoxb-... python create_finviz_channels.py
"""
import os
import sys
import requests

TOKEN = os.environ.get("SLACK_BOT_TOKEN")
if not TOKEN:
    print("Set SLACK_BOT_TOKEN first: export SLACK_BOT_TOKEN=xoxb-...")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

CHANNELS = [
    "finviz-canslim",
    "finviz-new-highs",
    "finviz-top-gainers",
    "finviz-top-losers",
    "finviz-major-news",
]

print("\nCreating Finviz Slack channels...\n")

results = {}
for name in CHANNELS:
    resp = requests.post(
        "https://slack.com/api/conversations.create",
        headers=HEADERS,
        json={"name": name, "is_private": False},
        timeout=10,
    )
    data = resp.json()

    if data.get("ok"):
        cid = data["channel"]["id"]
        results[name] = cid
        print(f"  ✓ #{name} → {cid}")
    elif data.get("error") == "name_taken":
        # Channel exists — look it up
        resp2 = requests.post(
            "https://slack.com/api/conversations.list",
            headers=HEADERS,
            json={"types": "public_channel", "limit": 1000},
            timeout=10,
        )
        found = False
        for ch in resp2.json().get("channels", []):
            if ch["name"] == name:
                cid = ch["id"]
                results[name] = cid
                print(f"  ✓ #{name} → {cid} (already exists)")
                found = True
                break
        if not found:
            print(f"  ✗ #{name} — name_taken but couldn't find ID")
    else:
        print(f"  ✗ #{name} — {data.get('error')}")

print(f"\n{'=' * 50}")
print("CHANNEL_MAP entries for finviz_scan.py:\n")
print(f'    "canslim_growth":     "{results.get("finviz-canslim", "MISSING")}",  # #finviz-canslim')
print(f'    "new_highs":          "{results.get("finviz-new-highs", "MISSING")}",  # #finviz-new-highs')
print(f'    "top_gainers":        "{results.get("finviz-top-gainers", "MISSING")}",  # #finviz-top-gainers')
print(f'    "top_losers":         "{results.get("finviz-top-losers", "MISSING")}",  # #finviz-top-losers')
print(f'    "major_news":         "{results.get("finviz-major-news", "MISSING")}",  # #finviz-major-news')
print(f'    "episodic_pivots":    "C0APETM14DT",  # #episodic-pivots')
print(f'    "bullsnort":          "C0APG8D3H0W",  # #bullsnort')
print(f'    "relative_strength":  "C0APBUEFG1Z",  # #relative-strength')
print(f'    "unusual_volume":     "C0APBUDJVNX",  # #volume-surges')
print()
