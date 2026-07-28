from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .client import DEFAULT_SCHEDULE_BASE_PATH, DEFAULT_SCHEDULE_HOST, DEFAULT_SCHEDULE_PORT, DEFAULT_SCHEDULE_SCHEME, FS42ScheduleClient, build_schedule_api_base_url
from .ffmpeg import FFMpegHLSCommandBuilder, PlayoutMode, StreamProfile
from .ffprobe import FFProbe
from .hls_harness import inspect_hls_output
from .paths import PathResolver
from .planner import BlockPlanner, PlannedBlock, plan_item_type_summary
from .playout_engine import resolve_block_playout

DEFAULT_API_BASE_URL = "http://127.0.0.1:4242"
DEFAULT_CHANNEL = "Example Channel"
DEFAULT_FS42_ROOT = "/mnt/fs42"
DEFAULT_SDTV_ROOT = "/mnt/media/SDTV"
DEFAULT_FFMPEG = "/usr/bin/ffmpeg"
DEFAULT_FFPROBE = "/usr/bin/ffprobe"
DEFAULT_SCHEDULE_TIMEZONE = "Europe/London"
HLS_TARGET_SEGMENT_DURATION = 2.0


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
    hls_start_time_offset: float | None = None
    hls_append: bool = False
    hls_boundary_starts: tuple[int, ...] = ()
    schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE
    stream_profile: StreamProfile = "direct"
    playout_mode: PlayoutMode = "hls-primary"
    schedule: Mapping[str, Any] | None = None
    selected_block_index: int | None = None
    selected_block_reason: str | None = None
    ffmpeg_status_callback: Callable[[Mapping[str, Any]], None] | None = None


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





def _schedule_summary_end(summary: Mapping[str, Any], channel: str) -> datetime | None:
    raw_single = summary.get("schedule_summary")
    if isinstance(raw_single, Mapping):
        end_value = raw_single.get("end")
        return _parse_datetime(end_value) if end_value else None
    raw_many = summary.get("schedule_summaries")
    if isinstance(raw_many, list):
        for item in raw_many:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("network_id") or item.get("network_name") or "") != channel:
                continue
            end_value = item.get("end")
            return _parse_datetime(end_value) if end_value else None
    return None


def _stale_schedule_state(summary: Mapping[str, Any] | None, *, channel: str, now: datetime | None, schedule_timezone: str | None) -> dict[str, Any] | None:
    if not isinstance(summary, Mapping):
        return None
    end = _schedule_summary_end(summary, channel)
    if end is None:
        return None
    schedule_now = _schedule_now(now, schedule_timezone)
    comparable_now = schedule_now if end.tzinfo is None else _coerce_now_for([(0, {}, None, end)], schedule_now)
    if comparable_now <= end:
        return None
    return {
        "active": True,
        "channel": channel,
        "schedule_now": comparable_now.isoformat(),
        "summary_end": end.isoformat(),
        "reason": "summary-expired",
    }


def _build_stale_placeholder_schedule(channel: str, *, now: datetime | None, duration_limit: float, schedule_timezone: str | None) -> dict[str, Any]:
    start = _schedule_now(now, schedule_timezone)
    fallback_duration = max(duration_limit, 60.0)
    end = start + timedelta(seconds=fallback_duration)
    return {
        "network_name": channel,
        "schedule_blocks": [
            {
                "title": "Schedule stale - BRB",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "plan": [
                    {
                        "path": "runtime/brb.png",
                        "duration": fallback_duration,
                        "skip": 0,
                        "is_stream": False,
                        "content_type": "slate",
                        "media_type": "image",
                    }
                ],
            }
        ],
    }

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
        if config.playout_mode not in {"hls-primary", "ts-primary"}:
            raise ValueError("playout_mode must be 'hls-primary' or 'ts-primary'")
        config.output_dir.mkdir(parents=True, exist_ok=True)

        if config.schedule is not None:
            schedule = config.schedule
            blocks = schedule.get("schedule_blocks")
            if not isinstance(blocks, list) or not blocks:
                raise ValueError("schedule contains no schedule_blocks")
            selected_index = int(config.selected_block_index or 0)
            if selected_index < 0 or selected_index >= len(blocks) or not isinstance(blocks[selected_index], Mapping):
                raise ValueError(f"selected_block_index {selected_index} is not present in schedule")
            selected = SelectedBlock(index=selected_index, reason=config.selected_block_reason or "shared", block=blocks[selected_index])
            stale_schedule = None
        else:
            schedule = self.client.fetch_schedule(config.channel, expected_blocks=None)
            summary = None
            if hasattr(self.client, "fetch_schedule_summary"):
                try:
                    summary = self.client.fetch_schedule_summary(config.channel)
                except Exception:
                    summary = None
            stale_schedule = _stale_schedule_state(summary, channel=config.channel, now=config.now, schedule_timezone=config.schedule_timezone)
            if stale_schedule and stale_schedule.get("active"):
                schedule = _build_stale_placeholder_schedule(config.channel, now=config.now, duration_limit=config.duration_limit, schedule_timezone=config.schedule_timezone)
                selected = SelectedBlock(index=0, reason="stale", block=schedule["schedule_blocks"][0])
            else:
                selected = select_current_or_next_block(schedule, now=config.now, schedule_timezone=config.schedule_timezone)
        playout = resolve_block_playout(
            selected.block,
            now=config.now,
            schedule_timezone=config.schedule_timezone,
            selection_index=selected.index,
            selection_reason=selected.reason,
        )
        planned = self.planner.plan_playout_items(
            playout.render_state.plan,
            title=playout.render_state.title,
            start_time=playout.render_state.start_time,
            end_time=playout.render_state.end_time,
            source=playout.render_state.source,
        )
        commands: list[list[str]] = []
        render_blocks: list[PlannedBlock] = [planned]
        render_duration_limits: list[float] = [config.duration_limit]
        ts_primary_blocks: list[PlannedBlock] = []
        ts_primary_duration_limits: list[float] = []
        hls_start_number = config.hls_start_number
        boundary_starts = sorted({int(number) for number in config.hls_boundary_starts if int(number) >= 0})
        output_slug = FFMpegHLSCommandBuilder._slug(config.output_name or planned.title)
        transport_stream = config.output_dir / f"{output_slug}.playout.ts"
        transport_stream_parts: list[Path] = []
        if config.playout_mode == "ts-primary":
            remaining_duration = max(0.0, float(config.duration_limit))
            for item_index, item in enumerate(planned.items):
                if remaining_duration <= 0:
                    break
                item_duration = max(0.0, float(item.duration or item.probe.duration or remaining_duration))
                item_duration_limit = min(remaining_duration, item_duration) if item_duration > 0 else remaining_duration
                if item_duration_limit <= 0:
                    continue
                transport_stream_part = config.output_dir / f"{output_slug}.item{item_index:05d}.playout.ts"
                transport_stream_parts.append(transport_stream_part)
                ts_primary_blocks.append(_single_item_block(planned, item, item_index=item_index))
                ts_primary_duration_limits.append(item_duration_limit)
                commands.append(
                    self.builder.build_transport_stream_and_hls(
                        ts_primary_blocks[-1],
                        output_dir=config.output_dir,
                        output_name=config.output_name or planned.title,
                        transport_stream_path=transport_stream_part,
                        duration_limit=item_duration_limit,
                        hls_start_number=config.hls_start_number,
                        hls_start_time_offset=config.hls_start_time_offset if config.stream_profile == "jellyfin" else None,
                        hls_append=config.hls_append or item_index > 0,
                        stream_profile=config.stream_profile,
                    )
                )
                remaining_duration -= item_duration_limit
        else:
            commands.append(
                self.builder.build(
                    planned,
                    output_dir=config.output_dir,
                    duration_limit=config.duration_limit,
                    output_name=config.output_name,
                    hls_start_number=config.hls_start_number,
                    hls_start_time_offset=config.hls_start_time_offset if config.stream_profile == "jellyfin" else None,
                    hls_append=config.hls_append,
                    stream_profile=config.stream_profile,
                )
            )

        diagnostics = _diagnostics(
            status="dry-run" if config.dry_run else "ok",
            channel=config.channel,
            schedule=schedule,
            render_state=playout.render_state,
            planned=planned,
            output_dir=config.output_dir,
            duration_limit=config.duration_limit,
            command=commands[0] if commands else [],
            output_name=config.output_name,
            hls_start_number=config.hls_start_number,
            hls_start_time_offset=config.hls_start_time_offset,
            hls_append=config.hls_append,
            stream_profile=config.stream_profile,
            catch_up=playout.catch_up,
            playout=playout.snapshot,
            stale_schedule=stale_schedule,
        )
        diagnostics["render_mode"] = "ts-primary" if config.playout_mode == "ts-primary" else "block-concat"
        diagnostics["stream_profile"] = config.stream_profile
        diagnostics["playout_mode"] = config.playout_mode
        diagnostics["playout_artifacts"] = {"transport_stream": str(transport_stream)} if config.playout_mode == "ts-primary" else {}
        diagnostics["commands"] = commands
        diagnostics["item_command_count"] = len(commands)
        if config.dry_run:
            return diagnostics

        ffmpeg_runs: list[dict[str, Any]] = []
        executed_commands: list[list[str]] = []
        final_returncode = 0
        command_hls_start_number = config.hls_start_number
        command_hls_start_time_offset = config.hls_start_time_offset
        final_playlist_state: dict[str, Any] | None = None
        playlist = Path(diagnostics["playlist"])
        if config.playout_mode == "ts-primary":
            if transport_stream.exists():
                transport_stream.unlink()
            for run_index, _preview_command in enumerate(commands):
                appended_run = config.hls_append or run_index > 0
                run_boundary_start = command_hls_start_number if appended_run and config.stream_profile != "jellyfin" else None
                command = self.builder.build_transport_stream_and_hls(
                    ts_primary_blocks[run_index],
                    output_dir=config.output_dir,
                    output_name=config.output_name or planned.title,
                    transport_stream_path=transport_stream_parts[run_index],
                    duration_limit=ts_primary_duration_limits[run_index],
                    hls_start_number=command_hls_start_number,
                    hls_start_time_offset=command_hls_start_time_offset if config.stream_profile == "jellyfin" else None,
                    hls_append=appended_run,
                    stream_profile=config.stream_profile,
                )
                executed_commands.append(command)
                completed = _run_ffmpeg_command(command, playlist=playlist, normalize_jellyfin=config.stream_profile == "jellyfin" and config.playout_mode == "ts-primary", status_callback=config.ffmpeg_status_callback)
                ffmpeg_runs.append(
                    {
                        "index": run_index,
                        "stage": "transport+hls",
                        "returncode": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                        "hls_start_number": command_hls_start_number,
                        "hls_start_time_offset": command_hls_start_time_offset,
                    }
                )
                final_returncode = completed.returncode
                part_path = transport_stream_parts[run_index] if run_index < len(transport_stream_parts) else None
                if isinstance(part_path, Path) and part_path.exists():
                    with transport_stream.open("ab") as combined_stream, part_path.open("rb") as part_stream:
                        shutil.copyfileobj(part_stream, combined_stream)
                    part_path.unlink(missing_ok=True)
                if playlist.exists():
                    if run_boundary_start is not None and run_boundary_start not in boundary_starts:
                        boundary_starts.append(run_boundary_start)
                        boundary_starts.sort()
                    if config.stream_profile == "jellyfin":
                        _normalize_jellyfin_live_playlist(playlist)
                        final_playlist_state = {
                            "discontinuity_sequence": 0,
                            "boundary_starts": [],
                            "first_visible_segment_number": None,
                        }
                    else:
                        final_playlist_state = _rewrite_live_playlist_boundaries(playlist, boundary_starts=boundary_starts)
                    previous_hls_start_number = command_hls_start_number
                    hls_start_number = _next_hls_start_number(playlist, fallback=hls_start_number)
                    command_hls_start_number = hls_start_number
                    if config.stream_profile == "jellyfin" and command_hls_start_time_offset is not None:
                        emitted_duration = _hls_segment_duration_since(playlist, start_number=previous_hls_start_number)
                        command_hls_start_time_offset += emitted_duration
                if completed.returncode != 0:
                    break
        else:
            for run_index, render_block in enumerate(render_blocks):
                appended_run = config.hls_append or run_index > 0
                command = self.builder.build(
                    render_block,
                    output_dir=config.output_dir,
                    duration_limit=render_duration_limits[run_index],
                    output_name=config.output_name,
                    hls_start_number=command_hls_start_number,
                    hls_start_time_offset=command_hls_start_time_offset if config.stream_profile == "jellyfin" else None,
                    hls_append=appended_run,
                    stream_profile=config.stream_profile,
                )
                run_boundary_start = command_hls_start_number if appended_run and config.stream_profile != "jellyfin" else None
                executed_commands.append(command)
                completed = _run_ffmpeg_command(command, playlist=playlist, normalize_jellyfin=config.stream_profile == "jellyfin" and config.playout_mode == "ts-primary", status_callback=config.ffmpeg_status_callback)
                run_info = {
                    "index": run_index,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "hls_start_number": command_hls_start_number,
                    "hls_start_time_offset": command_hls_start_time_offset,
                }
                ffmpeg_runs.append(run_info)
                final_returncode = completed.returncode
                if playlist.exists():
                    if run_boundary_start is not None and run_boundary_start not in boundary_starts:
                        boundary_starts.append(run_boundary_start)
                        boundary_starts.sort()
                    if config.stream_profile == "jellyfin":
                        _normalize_jellyfin_live_playlist(playlist)
                        final_playlist_state = {
                            "discontinuity_sequence": 0,
                            "boundary_starts": [],
                            "first_visible_segment_number": None,
                        }
                    else:
                        final_playlist_state = _rewrite_live_playlist_boundaries(playlist, boundary_starts=boundary_starts)
                    previous_hls_start_number = command_hls_start_number
                    hls_start_number = _next_hls_start_number(playlist, fallback=hls_start_number)
                    command_hls_start_number = hls_start_number
                    if config.stream_profile == "jellyfin" and command_hls_start_time_offset is not None:
                        emitted_duration = _hls_segment_duration_since(playlist, start_number=previous_hls_start_number)
                        command_hls_start_time_offset += emitted_duration
                if completed.returncode != 0:
                    break

        diagnostics["status"] = "ok" if final_returncode == 0 else "ffmpeg-error"
        diagnostics["commands"] = executed_commands
        diagnostics["ffmpeg"] = {
            "returncode": final_returncode,
            "runs": ffmpeg_runs,
            "stdout": ffmpeg_runs[-1]["stdout"] if ffmpeg_runs else "",
            "stderr": ffmpeg_runs[-1]["stderr"] if ffmpeg_runs else "",
        }
        playlist = Path(diagnostics["playlist"])
        if playlist.exists():
            if config.stream_profile == "jellyfin":
                _normalize_jellyfin_live_playlist(playlist)
                final_playlist_state = {
                    "discontinuity_sequence": 0,
                    "boundary_starts": [],
                    "first_visible_segment_number": None,
                }
            else:
                final_playlist_state = _rewrite_live_playlist_boundaries(playlist, boundary_starts=boundary_starts)
            inspection = inspect_hls_output(playlist)
            diagnostics["hls"] = {
                "playlist": str(inspection.playlist),
                "segments": [str(path) for path in inspection.segments],
                "segment_count": len(inspection.segments),
                "has_endlist": inspection.has_endlist,
            }
            emitted_duration = _hls_segment_duration_since(playlist, start_number=config.hls_start_number)
            diagnostics["emitted_media_duration"] = emitted_duration
            diagnostics["hls_next_start_number"] = _next_hls_start_number(playlist, fallback=hls_start_number)
            if config.hls_start_time_offset is not None:
                diagnostics["hls_segment_duration"] = emitted_duration
                diagnostics["hls_next_start_time_offset"] = config.hls_start_time_offset + emitted_duration
        diagnostics["hls_boundary_starts"] = [] if config.stream_profile == "jellyfin" else boundary_starts
        if isinstance(final_playlist_state, Mapping):
            diagnostics["hls_discontinuity_sequence"] = final_playlist_state.get("discontinuity_sequence")
        return diagnostics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded live FS42 schedule block as HLS.")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--duration-limit", type=float, default=120.0, help="maximum output duration in seconds")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/fs42stream-hls"))
    parser.add_argument("--schedule-scheme", default=DEFAULT_SCHEDULE_SCHEME)
    parser.add_argument("--schedule-host", default=DEFAULT_SCHEDULE_HOST)
    parser.add_argument("--schedule-port", type=int, default=DEFAULT_SCHEDULE_PORT)
    parser.add_argument("--schedule-base-path", default=DEFAULT_SCHEDULE_BASE_PATH)
    parser.add_argument("--api-base-url", default="", help="deprecated full schedule API URL override; prefer --schedule-* options")
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
    parser.add_argument("--output-name", help="stable HLS playlist/segment prefix, e.g. Your_Channel")
    parser.add_argument("--dry-run", action="store_true", help="validate and print command without running ffmpeg")
    parser.add_argument("--stream-profile", choices=("direct", "jellyfin"), default="direct", help="HLS packaging profile; jellyfin avoids discontinuity tags and offsets timestamps")
    parser.add_argument("--playout-mode", choices=("hls-primary", "ts-primary"), default="ts-primary", help="render directly to HLS or render TS first then package HLS")
    args = parser.parse_args(argv)

    runner = BlockRunner(
        client=FS42ScheduleClient(args.api_base_url or build_schedule_api_base_url(scheme=args.schedule_scheme, host=args.schedule_host, port=args.schedule_port, base_path=args.schedule_base_path), timeout=args.timeout),
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
        playout_mode=args.playout_mode,
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


def _run_ffmpeg_command(command: Sequence[str], *, playlist: Path, normalize_jellyfin: bool, status_callback: Callable[[Mapping[str, Any]], None] | None = None) -> subprocess.CompletedProcess[str]:
    started_at = datetime.now(timezone.utc).isoformat()
    if not normalize_jellyfin:
        if status_callback is None:
            return subprocess.run(command, check=False, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _notify_ffmpeg_status(status_callback, state="running", pid=process.pid, started_at=started_at)
        stdout, stderr = process.communicate()
        _notify_ffmpeg_status(status_callback, state="exited" if process.returncode == 0 else "error", pid=None, started_at=started_at, returncode=process.returncode, error=stderr if process.returncode != 0 else None)
        return subprocess.CompletedProcess(command, process.returncode, stdout=stdout, stderr=stderr)

    # Do not leave ffmpeg stdout/stderr connected to PIPE while we poll for
    # live Jellyfin playlist normalization. FFmpeg writes progress/log output
    # continuously; if the parent does not drain a PIPE, the child can block in
    # pipe_write and freeze the live profile indefinitely.
    with tempfile.TemporaryFile(mode="w+t") as stdout_file, tempfile.TemporaryFile(mode="w+t") as stderr_file:
        process = subprocess.Popen(command, shell=False, stdout=stdout_file, stderr=stderr_file, text=True)
        _notify_ffmpeg_status(status_callback, state="running", pid=process.pid, started_at=started_at)
        while process.poll() is None:
            if playlist.exists():
                _normalize_jellyfin_live_playlist(playlist)
            time.sleep(0.25)
        returncode = process.wait()
        if playlist.exists():
            _normalize_jellyfin_live_playlist(playlist)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
        _notify_ffmpeg_status(status_callback, state="exited" if returncode == 0 else "error", pid=None, started_at=started_at, returncode=returncode, error=stderr if returncode != 0 else None)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def _notify_ffmpeg_status(status_callback: Callable[[Mapping[str, Any]], None] | None, *, state: str, pid: int | None, started_at: str, returncode: int | None = None, error: str | None = None) -> None:
    if status_callback is None:
        return
    status_callback({
        "state": state,
        "pid": pid,
        "started_at": started_at,
        "last_exit_code": returncode,
        "last_error": error,
    })


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


def _hls_segment_duration_since(playlist: Path, *, start_number: int) -> float:
    """Return elapsed HLS duration from start_number through the current live playlist.

    FS42's HLS output uses a two-second target duration and forces 25 fps output
    with a 50-frame GOP/keyframe cadence for both normal blocks and boundary
    filler. A live playlist with hls_list_size=12 rolls older EXTINF lines out of
    the m3u8, so visible EXTINF values alone undercount long runs. Segment file
    numbers remain monotonic; for rolled-off full segments before the first
    visible segment, use the configured emitted cadence, then use actual EXTINF
    values for the visible tail where partial final segments can occur.
    """
    if not playlist.exists():
        return 0.0

    numbered_durations: list[tuple[int, float]] = []
    pending_duration: float | None = None
    for line in playlist.read_text(errors="replace").splitlines():
        text = line.strip()
        if text.startswith("#EXTINF:"):
            duration_text = text.removeprefix("#EXTINF:").split(",", 1)[0]
            try:
                pending_duration = float(duration_text)
            except ValueError:
                pending_duration = None
            continue
        if not text or text.startswith("#"):
            continue
        segment_number = _hls_segment_number(text)
        if segment_number is not None and segment_number >= start_number and pending_duration is not None:
            numbered_durations.append((segment_number, pending_duration))
        pending_duration = None

    total = 0.0
    expected_number = start_number
    for segment_number, duration in sorted(numbered_durations):
        if segment_number > expected_number:
            total += (segment_number - expected_number) * HLS_TARGET_SEGMENT_DURATION
        total += duration
        expected_number = max(expected_number, segment_number + 1)
    return total


def _hls_segment_number(segment_uri: str) -> int | None:
    stem = Path(segment_uri).stem
    suffix = stem.rsplit("_", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return None


def _normalize_jellyfin_live_playlist(playlist: Path) -> None:
    """Strip discontinuity markers for Jellyfin's remux-friendly live profile."""
    text = playlist.read_text(errors="replace")
    normalized_lines = [
        line
        for line in text.splitlines()
        if line.strip() != "#EXT-X-DISCONTINUITY" and not line.strip().startswith("#EXT-X-DISCONTINUITY-SEQUENCE")
    ]
    normalized = "\n".join(normalized_lines)
    if text.endswith("\n"):
        normalized += "\n"
    if normalized != text:
        playlist.write_text(normalized)


def _rewrite_live_playlist_boundaries(playlist: Path, *, boundary_starts: Sequence[int]) -> dict[str, Any]:
    """Rewrite live HLS discontinuity tags deterministically and track rollover state."""
    text = playlist.read_text(errors="replace")
    header_lines: list[str] = []
    footer_lines: list[str] = []
    entries: list[tuple[list[str], str]] = []
    pending_tags: list[str] = []
    seen_segment = False
    normalized_boundary_starts = sorted({int(number) for number in boundary_starts if int(number) >= 0})
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "#EXT-X-DISCONTINUITY" or stripped.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
            continue
        if stripped and not stripped.startswith("#"):
            entries.append((pending_tags.copy(), line))
            pending_tags.clear()
            seen_segment = True
            continue
        if not seen_segment and not pending_tags and not stripped.startswith("#EXTINF:"):
            header_lines.append(line)
            continue
        pending_tags.append(line)

    if pending_tags:
        footer_lines = pending_tags.copy()

    segment_numbers = [_hls_segment_number(uri) for _tags, uri in entries]
    visible_segment_numbers = [number for number in segment_numbers if number is not None]
    first_visible_segment_number = visible_segment_numbers[0] if visible_segment_numbers else None
    discontinuity_sequence = (
        sum(1 for number in normalized_boundary_starts if first_visible_segment_number is not None and number < first_visible_segment_number)
        if first_visible_segment_number is not None
        else 0
    )

    normalized_lines: list[str] = []
    inserted_sequence = False
    for line in header_lines:
        normalized_lines.append(line)
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:") and discontinuity_sequence > 0:
            normalized_lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_sequence}")
            inserted_sequence = True
    if discontinuity_sequence > 0 and not inserted_sequence:
        normalized_lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_sequence}")

    for (tags, uri), segment_number in zip(entries, segment_numbers):
        if (
            segment_number is not None
            and first_visible_segment_number is not None
            and segment_number != first_visible_segment_number
            and segment_number in normalized_boundary_starts
        ):
            normalized_lines.append("#EXT-X-DISCONTINUITY")
        normalized_lines.extend(tags)
        normalized_lines.append(uri)
    normalized_lines.extend(footer_lines)

    normalized = "\n".join(normalized_lines)
    if text.endswith("\n"):
        normalized += "\n"
    if normalized != text:
        playlist.write_text(normalized)
    return {
        "discontinuity_sequence": discontinuity_sequence,
        "boundary_starts": normalized_boundary_starts,
        "first_visible_segment_number": first_visible_segment_number,
    }


def _diagnostics(
    *,
    status: str,
    channel: str,
    schedule: Mapping[str, Any],
    render_state: Any,
    planned: PlannedBlock,
    output_dir: Path,
    duration_limit: float,
    command: list[str],
    output_name: str | None = None,
    hls_start_number: int = 0,
    hls_start_time_offset: float | None = None,
    hls_append: bool = False,
    stream_profile: StreamProfile = "direct",
    catch_up: Mapping[str, Any] | None = None,
    playout: Mapping[str, Any] | None = None,
    stale_schedule: Mapping[str, Any] | None = None,
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
        "hls_start_time_offset": hls_start_time_offset,
        "hls_append": hls_append,
        "stream_profile": stream_profile,
        "catch_up": dict(catch_up or {"applied": False}),
        "playout": dict(playout or {}),
        "stale_schedule": dict(stale_schedule or {}),
        "stale_schedule": dict(stale_schedule or {}),
        **type_summary,
        "selection": {
            "index": render_state.selection.index,
            "reason": render_state.selection.reason,
            "block_title": render_state.selection.block_title,
            "start_time": render_state.selection.start_time,
            "end_time": render_state.selection.end_time,
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
