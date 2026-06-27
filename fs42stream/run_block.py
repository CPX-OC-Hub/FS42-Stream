from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Mapping, Sequence

from .client import FS42ScheduleClient
from .ffmpeg import FFMpegHLSCommandBuilder, StreamProfile
from .ffprobe import FFProbe
from .hls_harness import inspect_hls_output
from .paths import PathResolver
from .planner import BlockPlanner, PlannedBlock, plan_item_type_summary

DEFAULT_API_BASE_URL = "http://192.168.10.252:4242"
DEFAULT_CHANNEL = "Sky One"
DEFAULT_FS42_ROOT = "/mnt/fs42"
DEFAULT_SDTV_ROOT = "/mnt/media/SDTV"
DEFAULT_FFMPEG = "/usr/bin/ffmpeg"
DEFAULT_FFPROBE = "/usr/bin/ffprobe"
DEFAULT_SCHEDULE_TIMEZONE = "Europe/London"


@dataclass(frozen=True)
class SelectedBlock:
    index: int
    reason: str
    block: Mapping[str, Any]


@dataclass(frozen=True)
class BlockRunConfig:
    channel: str = DEFAULT_CHANNEL
    duration_limit: float = 120.0
    output_dir: Path = Path("/tmp/fs42stream-hls")
    now: datetime | None = None
    dry_run: bool = False
    output_name: str | None = None
    hls_start_number: int = 0
    hls_append: bool = False
    schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE
    stream_profile: StreamProfile = "direct"


def select_current_or_next_block(schedule: Mapping[str, Any], *, now: datetime | None = None, schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE) -> SelectedBlock:
    """Return the current FS42 schedule block, or the next future block.

    Selection is deterministic: schedule order is preserved for tie-breaking, the
    first block where ``start_time <= now < end_time`` wins, and otherwise the
    future block with the earliest start time wins. If all blocks are in the
    past, the last block is selected with reason ``last`` so callers receive a
    clear diagnostic rather than a hidden fallback.
    """

    current_time = _schedule_now(now, schedule_timezone)
    blocks = schedule.get("schedule_blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("schedule contains no schedule_blocks")

    parsed: list[tuple[int, Mapping[str, Any], datetime | None, datetime | None]] = []
    for index, raw_block in enumerate(blocks):
        if not isinstance(raw_block, Mapping):
            raise ValueError(f"schedule block {index} is not a JSON object")
        start = _parse_datetime(raw_block.get("start_time"))
        end = _parse_datetime(raw_block.get("end_time"))
        parsed.append((index, raw_block, start, end))

    comparable_now = _coerce_now_for(parsed, current_time)
    for index, block, start, end in parsed:
        if start is not None and end is not None and start <= comparable_now < end:
            return SelectedBlock(index=index, reason="current", block=block)

    future = [(start, index, block) for index, block, start, _end in parsed if start is not None and start >= comparable_now]
    if future:
        _start, index, block = min(future, key=lambda row: (row[0], row[1]))
        return SelectedBlock(index=index, reason="next", block=block)

    index, block, _start, _end = parsed[-1]
    return SelectedBlock(index=index, reason="last", block=block)


class BlockRunner:
    """Fetch, select, validate, and run one bounded FS42 block as HLS."""

    def __init__(
        self,
        *,
        client: FS42ScheduleClient | None = None,
        planner: BlockPlanner | None = None,
        builder: FFMpegHLSCommandBuilder | None = None,
    ) -> None:
        self.client = client or FS42ScheduleClient(DEFAULT_API_BASE_URL)
        self.planner = planner or BlockPlanner(PathResolver(fs42_root=DEFAULT_FS42_ROOT, sdtv_root=DEFAULT_SDTV_ROOT), FFProbe(DEFAULT_FFPROBE))
        self.builder = builder or FFMpegHLSCommandBuilder(DEFAULT_FFMPEG)

    def run(self, config: BlockRunConfig) -> dict[str, Any]:
        if config.duration_limit <= 0:
            raise ValueError("duration_limit must be positive")
        if config.stream_profile not in {"direct", "jellyfin"}:
            raise ValueError("stream_profile must be 'direct' or 'jellyfin'")
        config.output_dir.mkdir(parents=True, exist_ok=True)

        schedule = self.client.fetch_schedule(config.channel, expected_blocks=None)
        selected = select_current_or_next_block(schedule, now=config.now, schedule_timezone=config.schedule_timezone)
        catch_up_block, catch_up = _catch_up_block_to_wallclock(selected.block, now=config.now, schedule_timezone=config.schedule_timezone)
        planned = self.planner.plan_block(catch_up_block)
        commands: list[list[str]] = []
        item_blocks: list[PlannedBlock] = []
        hls_start_number = config.hls_start_number
        remaining_budget = config.duration_limit
        if config.stream_profile == "jellyfin":
            item_blocks.append(planned)
            command = self.builder.build(
                planned,
                output_dir=config.output_dir,
                duration_limit=config.duration_limit,
                output_name=config.output_name,
                hls_start_number=config.hls_start_number,
                hls_append=False,
                stream_profile=config.stream_profile,
            )
            commands.append(command)
        else:
            for item_index, item in enumerate(planned.items):
                if remaining_budget <= 0:
                    break
                item_duration_limit = min(item.duration, remaining_budget) if item.duration > 0 else remaining_budget
                item_block = _single_item_block(planned, item, item_index=item_index)
                item_blocks.append(item_block)
                command = self.builder.build(
                    item_block,
                    output_dir=config.output_dir,
                    duration_limit=item_duration_limit,
                    output_name=config.output_name,
                    hls_start_number=hls_start_number,
                    hls_append=config.hls_append or item_index > 0,
                    stream_profile=config.stream_profile,
                )
                commands.append(command)
                remaining_budget -= item_duration_limit

        diagnostics = _diagnostics(
            status="dry-run" if config.dry_run else "ok",
            channel=config.channel,
            schedule=schedule,
            selected=selected,
            planned=planned,
            output_dir=config.output_dir,
            duration_limit=config.duration_limit,
            command=commands[0] if commands else [],
            output_name=config.output_name,
            hls_start_number=config.hls_start_number,
            hls_append=config.hls_append,
            stream_profile=config.stream_profile,
            catch_up=catch_up,
        )
        diagnostics["render_mode"] = "jellyfin-monotonic-block" if config.stream_profile == "jellyfin" else "sequential-plan-items"
        diagnostics["stream_profile"] = config.stream_profile
        diagnostics["commands"] = commands
        diagnostics["item_command_count"] = len(commands)
        if config.dry_run:
            return diagnostics

        ffmpeg_runs: list[dict[str, Any]] = []
        final_returncode = 0
        for run_index, command in enumerate(commands):
            completed = subprocess.run(command, check=False, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            run_info = {
                "index": run_index,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
            ffmpeg_runs.append(run_info)
            final_returncode = completed.returncode
            playlist = Path(diagnostics["playlist"])
            if playlist.exists():
                hls_start_number = _next_hls_start_number(playlist, fallback=hls_start_number)
            if completed.returncode != 0:
                break

        diagnostics["status"] = "ok" if final_returncode == 0 else "ffmpeg-error"
        diagnostics["ffmpeg"] = {
            "returncode": final_returncode,
            "runs": ffmpeg_runs,
            "stdout": ffmpeg_runs[-1]["stdout"] if ffmpeg_runs else "",
            "stderr": ffmpeg_runs[-1]["stderr"] if ffmpeg_runs else "",
        }
        playlist = Path(diagnostics["playlist"])
        if playlist.exists():
            inspection = inspect_hls_output(playlist)
            diagnostics["hls"] = {
                "playlist": str(inspection.playlist),
                "segments": [str(path) for path in inspection.segments],
                "segment_count": len(inspection.segments),
                "has_endlist": inspection.has_endlist,
            }
            diagnostics["hls_next_start_number"] = _next_hls_start_number(playlist, fallback=hls_start_number)
        return diagnostics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded live FS42 schedule block as HLS.")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--duration-limit", type=float, default=120.0, help="maximum output duration in seconds")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/fs42stream-hls"))
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--fs42-root", type=Path, default=Path(DEFAULT_FS42_ROOT))
    parser.add_argument("--sdtv-root", type=Path, default=Path(DEFAULT_SDTV_ROOT))
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE)
    parser.add_argument("--video-encoder", default="libx264", help="video encoder, e.g. libx264 or h264_vaapi")
    parser.add_argument("--vaapi-device", help="VAAPI device path, e.g. /dev/dri/renderD128")
    parser.add_argument("--schedule-timezone", default=DEFAULT_SCHEDULE_TIMEZONE, help="timezone for naive FS42 schedule timestamps, e.g. Europe/London")
    parser.add_argument("--fallback-slate-video", type=Path, help="optional prebuilt video used instead of generated black slate for runtime/off-air image entries")
    parser.add_argument("--timeout", type=float, default=10.0, help="FS42 API timeout in seconds")
    parser.add_argument("--now", help="override current time for deterministic tests, e.g. 2026-06-17T10:05:00")
    parser.add_argument("--output-name", help="stable HLS playlist/segment prefix, e.g. Sky_One")
    parser.add_argument("--dry-run", action="store_true", help="validate and print command without running ffmpeg")
    parser.add_argument("--stream-profile", choices=("direct", "jellyfin"), default="direct", help="HLS packaging profile; jellyfin avoids discontinuity tags and offsets timestamps")
    args = parser.parse_args(argv)

    runner = BlockRunner(
        client=FS42ScheduleClient(args.api_base_url, timeout=args.timeout),
        planner=BlockPlanner(PathResolver(fs42_root=args.fs42_root, sdtv_root=args.sdtv_root), FFProbe(args.ffprobe), fallback_slate_video=args.fallback_slate_video),
        builder=FFMpegHLSCommandBuilder(args.ffmpeg, video_encoder=args.video_encoder, vaapi_device=args.vaapi_device),
    )
    config = BlockRunConfig(
        channel=args.channel,
        duration_limit=args.duration_limit,
        output_dir=args.output_dir,
        now=_parse_datetime(args.now) if args.now else None,
        dry_run=args.dry_run,
        output_name=args.output_name,
        schedule_timezone=args.schedule_timezone,
        stream_profile=args.stream_profile,
    )
    try:
        diagnostics = runner.run(config)
    except Exception as exc:
        error = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
        print(json.dumps(error, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(diagnostics, indent=2, sort_keys=True))
    return 0


def _single_item_block(block: PlannedBlock, item: Any, *, item_index: int) -> PlannedBlock:
    return PlannedBlock(
        title=block.title,
        start_time=block.start_time,
        end_time=block.end_time,
        source=block.source,
        items=[item],
    )


def _next_hls_start_number(playlist: Path, *, fallback: int) -> int:
    if not playlist.exists():
        return fallback
    highest = fallback - 1
    for line in playlist.read_text(errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        stem = Path(text).stem
        suffix = stem.rsplit("_", 1)[-1]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return max(fallback, highest + 1)


def _diagnostics(
    *,
    status: str,
    channel: str,
    schedule: Mapping[str, Any],
    selected: SelectedBlock,
    planned: PlannedBlock,
    output_dir: Path,
    duration_limit: float,
    command: list[str],
    output_name: str | None = None,
    hls_start_number: int = 0,
    hls_append: bool = False,
    stream_profile: StreamProfile = "direct",
    catch_up: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    output_slug = FFMpegHLSCommandBuilder._slug(output_name or planned.title)
    playlist = output_dir / f"{output_slug}.m3u8"
    type_summary = plan_item_type_summary(planned.items)
    diagnostics: dict[str, Any] = {
        "status": status,
        "channel": channel,
        "network_name": schedule.get("network_name"),
        "duration_limit": duration_limit,
        "output_dir": str(output_dir),
        "playlist": str(playlist),
        "hls_start_number": hls_start_number,
        "hls_append": hls_append,
        "stream_profile": stream_profile,
        "catch_up": dict(catch_up or {"applied": False}),
        **type_summary,
        "selection": {
            "index": selected.index,
            "reason": selected.reason,
            "block_title": planned.title,
            "start_time": planned.start_time,
            "end_time": planned.end_time,
        },
        "plan": [
            {
                "index": index,
                "resolved_path": str(item.resolved_path),
                "skip": item.skip,
                "duration": item.duration,
                "type": item.fs42_type,
                "source_type": item.source.get("type"),
                "content_type": item.source.get("content_type"),
                "media_type": item.source.get("media_type"),
                "input_kind": item.input_kind,
                "ffmpeg_input": item.ffmpeg_input,
                "runtime_action": item.runtime_action,
                "diagnostic": item.diagnostic,
                "probe": {
                    "duration": item.probe.duration,
                    "width": item.probe.width,
                    "height": item.probe.height,
                    "fps": item.probe.fps,
                    "audio_sample_rate": item.probe.audio_sample_rate,
                    "audio_channels": item.probe.audio_channels,
                },
            }
            for index, item in enumerate(planned.items)
        ],
        "command": command,
    }
    return diagnostics


def _catch_up_block_to_wallclock(block: Mapping[str, Any], *, now: datetime | None, schedule_timezone: str | None) -> tuple[Mapping[str, Any], dict[str, Any]]:
    if now is None:
        return block, {"applied": False, "reason": "no_now"}
    block_start = _parse_datetime(block.get("start_time"))
    if block_start is None:
        return block, {"applied": False, "reason": "missing_block_start"}
    schedule_now = _schedule_now(now, schedule_timezone)
    elapsed = (schedule_now - block_start).total_seconds()
    if elapsed <= 0:
        return block, {"applied": False, "reason": "before_or_at_block_start", "block_elapsed": max(0.0, elapsed)}
    raw_plan = block.get("plan")
    if not isinstance(raw_plan, list):
        return block, {"applied": False, "reason": "missing_plan", "block_elapsed": elapsed}

    cursor = 0.0
    for index, item in enumerate(raw_plan):
        if not isinstance(item, Mapping):
            continue
        duration = _float_or_zero(item.get("duration"))
        item_end = cursor + duration
        if cursor <= elapsed < item_end:
            offset_in_item = elapsed - cursor
            adjusted_first = dict(item)
            original_skip = _float_or_zero(adjusted_first.get("skip"))
            adjusted_first["skip"] = original_skip + offset_in_item
            adjusted_first["duration"] = max(0.0, duration - offset_in_item)
            adjusted_first["catch_up_offset"] = offset_in_item
            trimmed_plan = [adjusted_first]
            trimmed_plan.extend(dict(next_item) for next_item in raw_plan[index + 1 :] if isinstance(next_item, Mapping))
            caught_block = dict(block)
            caught_block["plan"] = trimmed_plan
            return caught_block, {
                "applied": True,
                "schedule_now": schedule_now.isoformat(),
                "block_elapsed": elapsed,
                "start_plan_index": index,
                "offset_in_item": offset_in_item,
                "original_skip": original_skip,
                "media_seek": original_skip + offset_in_item,
                "remaining_item_duration": max(0.0, duration - offset_in_item),
                "dropped_plan_items": index,
                "current_content_type": item.get("content_type") or item.get("type") or item.get("media_type"),
                "current_path": item.get("path") or item.get("realpath"),
            }
        cursor = item_end
    return block, {"applied": False, "reason": "after_plan_end", "block_elapsed": elapsed, "plan_duration": cursor}


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid schedule datetime: {value!r}") from exc


def _schedule_now(now: datetime | None, schedule_timezone: str | None) -> datetime:
    if schedule_timezone:
        zone = ZoneInfo(schedule_timezone)
        source = now or datetime.now(timezone.utc)
        if source.tzinfo is None:
            source = source.replace(tzinfo=zone)
        return source.astimezone(zone).replace(tzinfo=None)
    return now or datetime.now()


def _coerce_now_for(parsed: Sequence[tuple[int, Mapping[str, Any], datetime | None, datetime | None]], now: datetime) -> datetime:
    has_aware = any((start and start.tzinfo) or (end and end.tzinfo) for _idx, _block, start, end in parsed)
    if has_aware and now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    if not has_aware and now.tzinfo is not None:
        return now.replace(tzinfo=None)
    return now


if __name__ == "__main__":
    raise SystemExit(main())
