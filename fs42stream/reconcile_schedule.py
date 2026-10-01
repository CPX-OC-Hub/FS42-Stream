from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .client import DEFAULT_SCHEDULE_API_BASE_URL, DEFAULT_SCHEDULE_CHANNEL, FS42ScheduleClient
from .schedule_recovery import _full_schedule_covers_now, _summary_is_fresh


def inspect_schedule(client: FS42ScheduleClient, *, channel: str, now: datetime, schedule_timezone: str | None) -> dict[str, Any]:
    """Compare the compact summary with full schedule coverage for one channel."""
    summary = dict(client.fetch_schedule_summary(channel))
    schedule = dict(client.fetch_schedule(channel))
    summary_fresh = _summary_is_fresh(summary, channel=channel, now=now, schedule_timezone=schedule_timezone)
    full_schedule_current = _full_schedule_covers_now(schedule, now=now, schedule_timezone=schedule_timezone)
    return {
        "channel": channel,
        "checked_at": now.isoformat(),
        "summary_fresh": summary_fresh,
        "full_schedule_current": full_schedule_current,
        "reconciliation_needed": not summary_fresh and full_schedule_current,
        "summary": summary,
        "full_schedule_block_count": len(schedule.get("schedule_blocks", [])) if isinstance(schedule.get("schedule_blocks"), list) else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and safely reconcile a FieldStation42 channel schedule summary.")
    parser.add_argument("--channel", default=DEFAULT_SCHEDULE_CHANNEL)
    parser.add_argument("--api-base-url", default=DEFAULT_SCHEDULE_API_BASE_URL)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--schedule-timezone", default="Europe/London", help="timezone for naive FS42 timestamps; pass an empty value to disable")
    parser.add_argument("--now", default="", help="ISO-8601 time for inspection/testing; defaults to current UTC")
    parser.add_argument("--apply", action="store_true", help="send the non-destructive FS42 add_time reconciliation after inspection")
    parser.add_argument("--reset", action="store_true", help="with --apply, reset the channel immediately before add_time; requires explicit approval")
    parser.add_argument("--wait-seconds", type=float, default=30.0, help="maximum seconds to wait for post-reconciliation validation")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args(argv)

    if args.timeout <= 0 or args.wait_seconds < 0 or args.poll_interval <= 0:
        parser.error("timeout and poll-interval must be positive; wait-seconds must be non-negative")
    if args.reset and not args.apply:
        parser.error("--reset requires --apply")

    now = _parse_now(args.now)
    schedule_timezone = args.schedule_timezone or None
    client = FS42ScheduleClient(args.api_base_url, timeout=args.timeout)
    try:
        before = inspect_schedule(client, channel=args.channel, now=now, schedule_timezone=schedule_timezone)
    except Exception as exc:
        _print({"channel": args.channel, "action": "inspection-failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1

    report: dict[str, Any] = dict(before)
    if not before["reconciliation_needed"]:
        report["action"] = "not-needed" if before["summary_fresh"] else "refused-no-current-full-schedule"
        _print(report)
        return 0 if before["summary_fresh"] else 2
    if not args.apply:
        report["action"] = "dry-run"
        _print(report)
        return 0

    try:
        actions: list[dict[str, Any]] = []
        if args.reset:
            actions.append({"reset": dict(client.reset_schedule(args.channel))})
        actions.append({"reconcile": dict(client.reconcile_schedule(args.channel))})
        validation = _wait_for_validation(
            client,
            channel=args.channel,
            now=now if args.now else None,
            schedule_timezone=schedule_timezone,
            wait_seconds=args.wait_seconds,
            poll_interval=args.poll_interval,
        )
    except Exception as exc:
        report.update({"action": "reconciliation-failed", "error": f"{type(exc).__name__}: {exc}"})
        _print(report)
        return 1

    report.update({"actions": actions, "validation": validation})
    if validation["summary_fresh"] and validation["full_schedule_current"]:
        report["action"] = "reconciled"
        _print(report)
        return 0
    report["action"] = "reconciliation-unvalidated"
    _print(report)
    return 3


def _wait_for_validation(
    client: FS42ScheduleClient,
    *,
    channel: str,
    now: datetime | None,
    schedule_timezone: str | None,
    wait_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    while True:
        inspection = inspect_schedule(
            client,
            channel=channel,
            now=now if now is not None else datetime.now(timezone.utc),
            schedule_timezone=schedule_timezone,
        )
        if inspection["summary_fresh"] and inspection["full_schedule_current"]:
            return inspection
        if time.monotonic() >= deadline:
            return inspection
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


def _parse_now(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--now must be ISO-8601") from exc


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    sys.exit(main())
