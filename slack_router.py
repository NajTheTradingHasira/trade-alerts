#!/usr/bin/env python3
"""
slack_router.py — Slack Channel Router
═══════════════════════════════════════════════
Routes alerts to signal-specific Slack channels AND the firehose.

Reads channel_config.json for channel IDs and route mappings.
Uses SLACK_BOT_TOKEN env var (or explicit token) for chat.postMessage.

Usage as module:
    from slack_router import SlackRouter

    router = SlackRouter.from_config("channel_config.json")
    router.send("__20_20__", "📐 *20/20 GROWTH SCREEN* — 12 hits")
    # → posts to #20-20-screener AND #firehose

Usage standalone (test):
    python slack_router.py --config channel_config.json --test
"""

import json
import os
import sys
from pathlib import Path

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:
    WebClient = None
    SlackApiError = None


class SlackRouter:
    """
    Routes messages to Slack channels based on event labels.

    Config format (channel_config.json):
    {
      "channels": {
        "20-20-screener":   "C0APG8ERD7Y",
        "40-40-screener":   "C0APJAFQLCA",
        "episodic-pivots":  "C0XXXXXXXXX",
        "firehose":         "C0ANZDPD4AU"
      },
      "routing": {
        "2020":             "20-20-screener",
        "4040":             "40-40-screener",
        "__20_20__":        "20-20-screener",
        "__40_40__":        "40-40-screener",
        "__episodic_pivot__": "episodic-pivots"
      }
    }

    Every message sent via send() or _post_message() to a signal channel
    is also forwarded to the firehose channel automatically when using send().
    """

    def __init__(self, client: "WebClient", channels: dict, routing: dict):
        self._client = client
        self._channels = channels      # name → channel ID
        self._routing = routing         # route_key → channel name
        self.firehose_id = channels.get("firehose")

    @classmethod
    def from_config(cls, config_path: str = "channel_config.json",
                    bot_token: str | None = None) -> "SlackRouter":
        """
        Build a SlackRouter from a config file and bot token.

        Token resolution order:
          1. Explicit bot_token argument
          2. SLACK_BOT_TOKEN env var
        """
        if WebClient is None:
            raise ImportError(
                "slack-sdk is required. Install with: pip install slack-sdk"
            )

        token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise ValueError(
                "No Slack token. Set SLACK_BOT_TOKEN env var or pass bot_token."
            )

        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")

        config = json.loads(path.read_text())
        channels = config.get("channels", {})
        routing = config.get("routing", {})

        client = WebClient(token=token)
        return cls(client, channels, routing)

    # ── Public API ──────────────────────────────────────────

    def send(self, route_key: str, text: str):
        """
        Post a message to the channel mapped to route_key AND to firehose.

        If route_key has no mapping, posts to firehose only.
        """
        channel_id = self._resolve_channel(route_key)

        if channel_id:
            self._post_message(channel_id, text)

        # Always forward to firehose
        if self.firehose_id and self.firehose_id != channel_id:
            self._post_message(self.firehose_id, text)

    def send_alert(self, route_key: str, text: str):
        """Alias for send()."""
        self.send(route_key, text)

    # ── Internal helpers (also used directly by screeners) ──

    def _resolve_channel(self, route_key: str) -> str | None:
        """
        Resolve a route key to a Slack channel ID.

        Lookup order:
          1. routing[route_key] → channels[name] → channel ID
          2. channels[route_key] → channel ID  (direct name match)
          3. route_key itself if it looks like a channel ID (starts with C)
        """
        # 1. Check routing table
        channel_name = self._routing.get(route_key)
        if channel_name and channel_name in self._channels:
            return self._channels[channel_name]

        # 2. Direct channel name lookup
        if route_key in self._channels:
            return self._channels[route_key]

        # 3. Raw channel ID passthrough
        if route_key.startswith("C") and len(route_key) >= 9:
            return route_key

        return None

    def _post_message(self, channel_id: str, text: str):
        """Post a single message to a Slack channel via chat.postMessage."""
        if not channel_id:
            return

        try:
            self._client.chat_postMessage(
                channel=channel_id,
                text=text,
                mrkdwn=True,
                unfurl_links=False,
                unfurl_media=False,
            )
        except SlackApiError as e:
            error = e.response.get("error", str(e))
            print(f"  ✗ Slack error ({channel_id}): {error}")
        except Exception as e:
            print(f"  ✗ Slack post failed ({channel_id}): {e}")

    def _channel_id_by_name(self, route_key: str) -> str | None:
        """Return the channel name (not ID) for a route key, for logging."""
        channel_name = self._routing.get(route_key)
        if channel_name:
            return channel_name
        if route_key in self._channels:
            return route_key
        return None

    def list_routes(self) -> dict:
        """Return a dict of route_key → (channel_name, channel_id) for inspection."""
        routes = {}
        for key, name in self._routing.items():
            cid = self._channels.get(name)
            routes[key] = {"channel": name, "id": cid}
        return routes


# ═══════════════════════════════════════════════════════════════
# Standalone test
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Slack Router — test & inspect")
    parser.add_argument("--config", default="channel_config.json",
                        help="Path to channel_config.json")
    parser.add_argument("--test", action="store_true",
                        help="Send a test message to firehose")
    parser.add_argument("--route", type=str, default=None,
                        help="Test a specific route key (e.g. __20_20__)")
    parser.add_argument("--list", action="store_true",
                        help="List all configured routes")
    args = parser.parse_args()

    try:
        router = SlackRouter.from_config(args.config)
    except (ImportError, ValueError, FileNotFoundError) as e:
        print(f"  ✗ {e}")
        sys.exit(1)

    if args.list:
        print("\n  Configured routes:")
        for key, info in router.list_routes().items():
            print(f"    {key:<22} → #{info['channel']:<20} ({info['id']})")
        print(f"    {'firehose':<22} → #firehose{' ' * 13} ({router.firehose_id})")
        print()
        return

    if args.route:
        cid = router._resolve_channel(args.route)
        name = router._channel_id_by_name(args.route)
        if cid:
            print(f"  Route {args.route} → #{name} ({cid})")
            msg = f"🧪 *Test alert* from slack_router.py\nRoute: `{args.route}` → `{cid}`"
            router.send(args.route, msg)
            print("  ✓ Test message sent")
        else:
            print(f"  ✗ No channel found for route key: {args.route}")
        return

    if args.test:
        if not router.firehose_id:
            print("  ✗ No firehose channel configured")
            sys.exit(1)
        msg = "🧪 *Slack Router test* — firehose connection OK"
        router._post_message(router.firehose_id, msg)
        print(f"  ✓ Test message sent to firehose ({router.firehose_id})")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
