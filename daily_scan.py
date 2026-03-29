#!/usr/bin/env python3
"""
Daily Market Close Scanner — Master Runner

Runs all screeners in sequence after market close (4:30 PM ET recommended).
Sends results to Slack via the multi-channel router.

Screeners included:
  1. 20/20 Growth Screen       → #20-20-screener
  2. 40/40 Growth Screen       → #40-40-screener
  3. Episodic Pivots            → #episodic-pivots
  4. Bull Snort                 → #bullsnort
  5. Volume Surges              → #volume-surges
  6. Relative Strength (Kell)   → #relative-strength
  7. Post-Market Movers         → #postmarket-movers

NOT included (runs separately before market open):
  - Pre-Market Movers           → #premarket-movers

Usage:
  python daily_scan.py                          # full scan, no Slack
  python daily_scan.py --slack                  # full scan + Slack alerts
  python daily_scan.py --slack --skip 2020 4040 # skip growth screens
  python daily_scan.py --only bullsnort volume  # run specific screeners
  python daily_scan.py --dry-run                # show what would run

Scheduling (Windows Task Scheduler):
  python daily_scan.py --create-task            # creates scheduled task XML

Environment Variables:
  SLACK_BOT_TOKEN    — Slack bot token for routing (required for --slack)
"""

import argparse
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Screener registry
# ---------------------------------------------------------------------------

SCREENERS = {
    "2020": {
        "name": "20/20 Growth Screen",
        "emoji": "📐",
        "module": "growth_screener",
        "function": "run_growth_screen",
        "kwargs": {"mode": "2020"},
        "channel": "#20-20-screener",
    },
    "4040": {
        "name": "40/40 Growth Screen",
        "emoji": "📐",
        "module": "growth_screener",
        "function": "run_growth_screen",
        "kwargs": {"mode": "4040"},
        "channel": "#40-40-screener",
    },
    "episodic": {
        "name": "Episodic Pivots",
        "emoji": "🔥",
        "module": "episodic_pivot_screener",
        "function": "run_episodic_pivot_screen",
        "kwargs": {},
        "channel": "#episodic-pivots",
    },
    "bullsnort": {
        "name": "Bull Snort",
        "emoji": "🐂",
        "module": "bullsnort_screener",
        "function": "run_bullsnort_screen",
        "kwargs": {},
        "channel": "#bullsnort",
    },
    "volume": {
        "name": "Unusual Volume",
        "emoji": "📈",
        "module": "volume_surge_screener",
        "function": "run_volume_surge_screen",
        "kwargs": {},
        "channel": "#volume-surges",
    },
    "rs": {
        "name": "Relative Strength (Up on Down Days)",
        "emoji": "💪",
        "module": "relative_strength_screener",
        "function": "run_relative_strength_screen",
        "kwargs": {},
        "channel": "#relative-strength",
        "returns_tuple": True,  # returns (df, down_days)
    },
    "postmarket": {
        "name": "Post-Market Movers",
        "emoji": "🌙",
        "module": "extended_hours_screener",
        "function": "run_extended_hours_screen",
        "kwargs": {"mode": "postmarket"},
        "channel": "#postmarket-movers",
        "returns_tuple": True,  # returns (winners, losers)
    },
}

# Slack functions per module (for sending results)
SLACK_SENDERS = {
    "growth_screener": "send_to_slack",
    "episodic_pivot_screener": "send_to_slack",
    "bullsnort_screener": "send_to_slack",
    "volume_surge_screener": "send_to_slack",
    "relative_strength_screener": "send_to_slack",
    "extended_hours_screener": "send_to_slack",
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_screener(key: str, config: dict, slack: bool = False, config_path: str = "channel_config.json") -> dict:
    """
    Run a single screener by key.

    Returns dict with: key, name, status, count, duration, error
    """
    info = SCREENERS[key]
    result = {
        "key": key,
        "name": info["name"],
        "emoji": info["emoji"],
        "channel": info["channel"],
        "status": "pending",
        "count": 0,
        "duration": 0,
        "error": None,
    }

    start = time.time()

    try:
        # Dynamic import
        mod = __import__(info["module"])
        run_fn = getattr(mod, info["function"])

        # Run screener
        output = run_fn(**info["kwargs"])

        # Handle different return types
        if info.get("returns_tuple"):
            if key == "rs":
                df, down_days = output
                result["count"] = len(df) if not df.empty else 0
            elif key == "postmarket":
                winners, losers = output
                result["count"] = len(winners) + len(losers)
                # Combine for reporting
                df = winners  # primary reference
            else:
                df = output[0] if isinstance(output, tuple) else output
                result["count"] = len(df) if hasattr(df, '__len__') else 0
        else:
            df = output
            result["count"] = len(df) if not df.empty else 0

        # Save CSV
        date_str = datetime.now().strftime("%Y%m%d")
        out_dir = Path("output") / "daily_scans" / date_str
        out_dir.mkdir(parents=True, exist_ok=True)

        if info.get("returns_tuple") and key == "postmarket":
            if not winners.empty:
                winners.to_csv(out_dir / f"{key}_winners.csv", index=False)
            if not losers.empty:
                losers.to_csv(out_dir / f"{key}_losers.csv", index=False)
        elif info.get("returns_tuple") and key == "rs":
            if not df.empty:
                df.to_csv(out_dir / f"{key}.csv", index=False)
        else:
            if not df.empty:
                df.to_csv(out_dir / f"{key}.csv", index=False)

        # Send to Slack
        if slack and result["count"] > 0:
            try:
                send_fn = getattr(mod, SLACK_SENDERS.get(info["module"], "send_to_slack"))
                if key == "rs":
                    send_fn(df, down_days, config_path)
                elif key == "postmarket":
                    send_fn(winners, losers, "postmarket", config_path)
                elif key in ("2020", "4040"):
                    send_fn(df, info["kwargs"]["mode"], config_path)
                else:
                    send_fn(df, config_path)
            except Exception as e:
                print(f"  ⚠ Slack send failed for {key}: {e}")

        result["status"] = "✓"

    except Exception as e:
        result["status"] = "✗"
        result["error"] = str(e)
        traceback.print_exc()

    result["duration"] = round(time.time() - start, 1)
    return result


def run_daily_scan(
    screener_keys: list[str] | None = None,
    skip_keys: list[str] | None = None,
    slack: bool = False,
    config_path: str = "channel_config.json",
    dry_run: bool = False,
):
    """
    Run all (or selected) screeners in sequence.
    """
    # Determine which screeners to run
    all_keys = list(SCREENERS.keys())

    if screener_keys:
        keys = [k for k in screener_keys if k in SCREENERS]
    else:
        keys = all_keys

    if skip_keys:
        keys = [k for k in keys if k not in skip_keys]

    total_start = time.time()
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\n" + "=" * 70)
    print(f"  DAILY MARKET CLOSE SCAN — {date_str}")
    print(f"  Screeners: {len(keys)} | Slack: {'ON' if slack else 'OFF'}")
    print("=" * 70)

    if dry_run:
        print("\n  [DRY RUN] Would run these screeners:\n")
        for k in keys:
            info = SCREENERS[k]
            print(f"    {info['emoji']} {info['name']} → {info['channel']}")
        print(f"\n  Total: {len(keys)} screeners")
        return

    # Slack: send start notification
    if slack:
        try:
            from slack_router import SlackRouter
            router = SlackRouter.from_config(config_path)
            start_msg = (
                f"🚀 *DAILY SCAN STARTED — {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n"
                f"> Running {len(keys)} screeners..."
            )
            router._post_message(router.firehose_id, start_msg)
        except Exception:
            pass

    # Run each screener
    results = []
    print()

    for i, key in enumerate(keys):
        info = SCREENERS[key]
        print(f"\n{'─' * 70}")
        print(f"  [{i + 1}/{len(keys)}] {info['emoji']} {info['name']}")
        print(f"{'─' * 70}\n")

        result = run_screener(key, info, slack=slack, config_path=config_path)
        results.append(result)

        print(f"\n  → {result['status']} {result['name']}: "
              f"{result['count']} hits in {result['duration']}s")

        if result["error"]:
            print(f"  → Error: {result['error']}")

    # Summary
    total_duration = round(time.time() - total_start, 1)
    total_hits = sum(r["count"] for r in results)
    passed = sum(1 for r in results if r["status"] == "✓")
    failed = sum(1 for r in results if r["status"] == "✗")

    print(f"\n\n{'=' * 70}")
    print(f"  DAILY SCAN COMPLETE — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'=' * 70}\n")

    # Results table
    print(f"  {'Screener':<30} {'Status':<8} {'Hits':<8} {'Time':<8}")
    print(f"  {'─' * 30} {'─' * 8} {'─' * 8} {'─' * 8}")
    for r in results:
        print(f"  {r['emoji']} {r['name']:<28} {r['status']:<8} {r['count']:<8} {r['duration']}s")

    print(f"\n  Total: {total_hits} hits across {passed} screeners "
          f"({failed} failed) in {total_duration}s")

    # Save summary
    date_str = datetime.now().strftime("%Y%m%d")
    out_dir = Path("output") / "daily_scans" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "date": datetime.now().isoformat(),
        "screeners_run": len(keys),
        "passed": passed,
        "failed": failed,
        "total_hits": total_hits,
        "duration_seconds": total_duration,
        "results": results,
    }

    import json
    (out_dir / "scan_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n  Summary saved to {out_dir / 'scan_summary.json'}")

    # Slack: send completion summary
    if slack:
        try:
            from slack_router import SlackRouter
            router = SlackRouter.from_config(config_path)

            lines = [f"  {r['emoji']} {r['name']}: {r['count']} hits" for r in results if r['count'] > 0]
            hits_summary = "\n".join(lines) if lines else "  No signals detected today."

            done_msg = (
                f"✅ *DAILY SCAN COMPLETE — {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n"
                f"> {total_hits} total hits across {passed} screeners in {total_duration}s\n"
                f"```\n{hits_summary}\n```"
            )
            router._post_message(router.firehose_id, done_msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------

TASK_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <Date>{date}</Date>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start_time}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>
          <Monday /><Tuesday /><Wednesday /><Thursday /><Friday />
        </DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python_path}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def create_task_scheduler_xml(
    task_name: str,
    description: str,
    hour: int,
    minute: int,
    arguments: str,
    output_path: str | None = None,
):
    """Generate a Windows Task Scheduler XML file."""
    import shutil

    python_path = shutil.which("python") or sys.executable
    working_dir = str(Path.cwd())
    now = datetime.now()
    start_time = f"{now.strftime('%Y-%m-%d')}T{hour:02d}:{minute:02d}:00"

    xml = TASK_XML_TEMPLATE.format(
        description=description,
        date=now.isoformat(),
        start_time=start_time,
        python_path=python_path,
        arguments=arguments,
        working_dir=working_dir,
    )

    out_file = output_path or f"{task_name}.xml"
    Path(out_file).write_text(xml, encoding="utf-16")
    print(f"\n✓ Task Scheduler XML saved to {out_file}")
    print(f"\n  To install, run in an elevated PowerShell:")
    print(f"    schtasks /Create /TN \"{task_name}\" /XML \"{Path(out_file).resolve()}\"")
    print(f"\n  To verify:")
    print(f"    schtasks /Query /TN \"{task_name}\"")
    print(f"\n  To delete:")
    print(f"    schtasks /Delete /TN \"{task_name}\" /F")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Daily Market Close Scanner — Master Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Screener keys:
  2020       20/20 Growth Screen
  4040       40/40 Growth Screen
  episodic   Episodic Pivots
  bullsnort  Bull Snort
  volume     Unusual Volume
  rs         Relative Strength (Up on Down Days)
  postmarket Post-Market Movers

Examples:
  python daily_scan.py                       # run all, no Slack
  python daily_scan.py --slack               # run all + Slack
  python daily_scan.py --only bullsnort rs   # specific screeners
  python daily_scan.py --skip 2020 4040      # skip growth screens
  python daily_scan.py --create-task         # set up Task Scheduler
        """,
    )
    parser.add_argument("--slack", action="store_true",
                        help="Send results to Slack via router")
    parser.add_argument("--config", default="channel_config.json",
                        help="Channel config path for Slack routing")
    parser.add_argument("--only", nargs="+", metavar="KEY",
                        help="Only run specific screeners")
    parser.add_argument("--skip", nargs="+", metavar="KEY",
                        help="Skip specific screeners")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would run without executing")
    parser.add_argument("--create-task", action="store_true",
                        help="Create Windows Task Scheduler XML files")
    parser.add_argument("--task-time", default="16:30",
                        help="Time for daily scan task (HH:MM, default: 16:30)")
    parser.add_argument("--premarket-time", default="08:30",
                        help="Time for premarket scan task (HH:MM, default: 08:30)")
    args = parser.parse_args()

    if args.create_task:
        h, m = map(int, args.task_time.split(":"))
        ph, pm = map(int, args.premarket_time.split(":"))

        print("=" * 60)
        print("CREATING TASK SCHEDULER CONFIGS")
        print("=" * 60)

        # Daily close scan (all except premarket)
        create_task_scheduler_xml(
            task_name="TradeAlerts_DailyScan",
            description="Run all screeners at market close and send alerts to Slack",
            hour=h, minute=m,
            arguments=f"daily_scan.py --slack --config {args.config}",
            output_path="task_daily_scan.xml",
        )

        # Premarket scan (separate task)
        create_task_scheduler_xml(
            task_name="TradeAlerts_PremarketScan",
            description="Run premarket movers scanner before market open",
            hour=ph, minute=pm,
            arguments=f"extended_hours_screener.py --mode premarket --slack --config {args.config}",
            output_path="task_premarket_scan.xml",
        )

        print(f"\n{'=' * 60}")
        print("Two tasks created:")
        print(f"  1. TradeAlerts_DailyScan    → {args.task_time} ET weekdays")
        print(f"  2. TradeAlerts_PremarketScan → {args.premarket_time} ET weekdays")
        print(f"{'=' * 60}")
        return

    # Run the scan
    run_daily_scan(
        screener_keys=args.only,
        skip_keys=args.skip,
        slack=args.slack,
        config_path=args.config,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
