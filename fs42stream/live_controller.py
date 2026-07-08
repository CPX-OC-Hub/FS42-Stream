from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .client import FS42ScheduleClient
from .ffmpeg import FFMpegHLSCommandBuilder, StreamProfile
from .ffprobe import FFProbe
from .paths import PathResolver
from .planner import BlockPlanner
from .run_block import (
    DEFAULT_API_BASE_URL,
    DEFAULT_CHANNEL,
    DEFAULT_FFMPEG,
    DEFAULT_FFPROBE,
    DEFAULT_FS42_ROOT,
    DEFAULT_SCHEDULE_TIMEZONE,
    DEFAULT_SDTV_ROOT,
    BlockRunConfig,
    BlockRunner,
    SelectedBlock,
    _hls_segment_duration_since,
    _next_hls_start_number,
    _normalize_jellyfin_live_playlist,
    _parse_datetime,
    _rewrite_live_playlist_boundaries,
    _schedule_now,
    select_current_or_next_block,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LiveControllerConfig:
    channel: str = DEFAULT_CHANNEL
    output_root: Path = Path("/tmp/fs42stream-live")
    max_blocks: int = 2
    duration_limit: float = 10.0
    now: datetime | None = None
    dry_run: bool = False
    status_callback: Callable[[Mapping[str, Any]], None] | None = None
    schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE
    max_recovery_attempts_per_block: int = 1
    clock: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], None] = time.sleep
    stream_profile: StreamProfile = "direct"


class ScheduleClient(Protocol):
    def fetch_schedule(self, channel: str, *, expected_blocks: int | None = None) -> Mapping[str, Any]: ...


class SingleBlockRunner(Protocol):
    def run(self, config: BlockRunConfig) -> Mapping[str, Any]: ...


class BoundaryFillerRunner(Protocol):
    def run(self, *, output_dir: Path, output_name: str, duration: float, hls_start_number: int, hls_start_time_offset: float | None = None, hls_append: bool = True, stream_profile: StreamProfile = "direct") -> Mapping[str, Any]: ...


class HLSBlackSlateFillerRunner:
    def __init__(self, ffmpeg: str = DEFAULT_FFMPEG) -> None:
        self.ffmpeg = ffmpeg

    def run(self, *, output_dir: Path, output_name: str, duration: float, hls_start_number: int, hls_start_time_offset: float | None = None, hls_append: bool = True, stream_profile: StreamProfile = "direct") -> Mapping[str, Any]:
        if duration <= 0:
            raise ValueError("duration must be positive")
        if hls_start_time_offset is not None and hls_start_time_offset < 0:
            raise ValueError("hls_start_time_offset must be non-negative")
        output_dir.mkdir(parents=True, exist_ok=True)
        playlist = output_dir / f"{output_name}.m3u8"
        segment_pattern = output_dir / f"{output_name}_%05d.ts"
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-y",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x480:r=25",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            _num(duration),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-g",
            "50",
            "-keyint_min",
            "50",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "12",
        ]
        if stream_profile != "jellyfin" or not hls_append:
            command.extend([
                "-start_number",
                str(hls_start_number),
            ])
        if stream_profile == "jellyfin" and hls_start_time_offset is not None:
            command.extend(["-output_ts_offset", _num(hls_start_time_offset)])
        if hls_append:
            hls_flags = "omit_endlist+append_list" if stream_profile == "jellyfin" else "omit_endlist+append_list+discont_start"
            command.extend(["-hls_flags", hls_flags])
        else:
            command.extend(["-hls_flags", "omit_endlist"])
        command.extend(["-hls_segment_filename", str(segment_pattern), str(playlist)])
        completed = subprocess.run(command, check=False, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if stream_profile == "jellyfin" and playlist.exists():
            _normalize_jellyfin_live_playlist(playlist)
        hls_next_start_number = _next_hls_start_number(playlist, fallback=hls_start_number)
        segment_duration = _hls_segment_duration_since(playlist, start_number=hls_start_number)
        return {
            "status": "ok" if completed.returncode == 0 else "ffmpeg-error",
            "playlist": str(playlist),
            "command": command,
            "hls_start_number": hls_start_number,
            "hls_start_time_offset": hls_start_time_offset,
            "hls_next_start_number": hls_next_start_number,
            "hls_segment_duration": segment_duration,
            "hls_next_start_time_offset": (hls_start_time_offset + segment_duration) if hls_start_time_offset is not None else None,
            "hls": {"playlist": str(playlist), "segment_count": max(0, hls_next_start_number - hls_start_number)},
            "ffmpeg": {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr},
        }


class LiveController:
    """Bounded live block lifecycle controller for stable per-channel HLS output."""

    def __init__(self, *, schedule_client: ScheduleClient | None = None, block_runner: SingleBlockRunner | None = None, filler_runner: BoundaryFillerRunner | None = None) -> None:
        self.schedule_client = schedule_client or FS42ScheduleClient(DEFAULT_API_BASE_URL)
        self.block_runner = block_runner or BlockRunner()
        self.filler_runner = filler_runner or HLSBlackSlateFillerRunner()

    def run(self, config: LiveControllerConfig) -> dict[str, Any]:
        if config.max_blocks <= 0:
            raise ValueError("max_blocks must be positive")
        if config.duration_limit <= 0:
            raise ValueError("duration_limit must be positive")
        if config.stream_profile not in {"direct", "jellyfin"}:
            raise ValueError("stream_profile must be 'direct' or 'jellyfin'")

        channel_output_dir = config.output_root / FFMpegHLSCommandBuilder._slug(config.channel)
        if config.stream_profile == "jellyfin":
            channel_output_dir = channel_output_dir / "jellyfin"
        channel_output_dir.mkdir(parents=True, exist_ok=True)

        events: list[dict[str, Any]] = []
        cursor = config.now
        simulated_cursor = config.now is not None
        last_index = -1

        hls_start_number = 0
        hls_start_time_offset = 0.0
        hls_boundary_starts: list[int] = []
        for ordinal in range(config.max_blocks):
            schedule = self.schedule_client.fetch_schedule(config.channel, expected_blocks=None)
            selection_now = cursor if simulated_cursor else config.clock()
            selected = _select_not_before(schedule, now=selection_now, minimum_index=last_index + 1, schedule_timezone=config.schedule_timezone) if simulated_cursor else select_current_or_next_block(schedule, now=selection_now, schedule_timezone=config.schedule_timezone)
            block_info = _block_info(selected)
            cleanup = clean_hls_outputs(channel_output_dir) if ordinal == 0 else []
            events.append(
                {
                    "event": "block_start",
                    "block_number": ordinal + 1,
                    "block": block_info,
                    "channel_output_dir": str(channel_output_dir),
                    "cleaned_stale_outputs": [str(path) for path in cleanup],
                }
            )
            _emit_live_status(
                config,
                status="running",
                channel_output_dir=channel_output_dir,
                events=events,
                schedule=schedule,
                selected=selected,
                schedule_now=selection_now,
            )
            block_start = _parse_datetime(selected.block.get("start_time"))
            block_end = _parse_datetime(selected.block.get("end_time"))
            run_now = selection_now if not simulated_cursor else (block_start or cursor)
            effective_duration_limit = config.duration_limit
            if not simulated_cursor and block_end is not None:
                schedule_now = _schedule_now(selection_now, config.schedule_timezone)
                remaining_seconds = max(0.0, (block_end - schedule_now).total_seconds())
                if remaining_seconds > 0:
                    effective_duration_limit = min(config.duration_limit, remaining_seconds)
            block_hls_append = ordinal > 0
            block_hls_start_number = hls_start_number
            diagnostics = self.block_runner.run(
                BlockRunConfig(
                    channel=config.channel,
                    duration_limit=effective_duration_limit,
                    output_dir=channel_output_dir,
                    now=run_now,
                    dry_run=config.dry_run,
                    output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                    hls_start_number=block_hls_start_number,
                    hls_start_time_offset=hls_start_time_offset if config.stream_profile == "jellyfin" else None,
                    hls_append=block_hls_append,
                    hls_boundary_starts=tuple(hls_boundary_starts),
                    schedule_timezone=config.schedule_timezone,
                    stream_profile=config.stream_profile,
                )
            )
            diagnostics_dict = dict(diagnostics)
            if isinstance(diagnostics_dict.get("hls_boundary_starts"), list):
                hls_boundary_starts = [int(number) for number in diagnostics_dict["hls_boundary_starts"]]
            hls_start_number = int(diagnostics_dict.get("hls_next_start_number") or hls_start_number)
            hls_start_time_offset = _next_hls_start_time_offset(diagnostics_dict, fallback=hls_start_time_offset)
            events.append(_complete_event(ordinal=ordinal, block=block_info, diagnostics=diagnostics_dict))
            _emit_live_status(
                config,
                status="running",
                channel_output_dir=channel_output_dir,
                events=events,
                schedule=schedule,
                selected=selected,
                schedule_now=selection_now,
            )

            if not simulated_cursor and _block_run_failed(diagnostics_dict):
                recovery_attempt = 0
                while recovery_attempt < config.max_recovery_attempts_per_block and block_end is not None:
                    recovery_attempt += 1
                    recovery_now = config.clock()
                    schedule_now = _schedule_now(recovery_now, config.schedule_timezone)
                    if schedule_now >= block_end:
                        break
                    recovery_selected = select_current_or_next_block(schedule, now=recovery_now, schedule_timezone=config.schedule_timezone)
                    if recovery_selected.index != selected.index:
                        break
                    recovery_block_info = _block_info(recovery_selected)
                    events.append(
                        {
                            "event": "block_recovery",
                            "block_number": ordinal + 1,
                            "attempt": recovery_attempt,
                            "reason": str(diagnostics_dict.get("status") or "ffmpeg-error"),
                            "block": recovery_block_info,
                            "recover_at": schedule_now.isoformat(),
                            "previous_status": diagnostics_dict.get("status"),
                            "previous_ffmpeg_returncode": _ffmpeg_returncode(diagnostics_dict),
                        }
                    )
                    _emit_live_status(
                        config,
                        status="recovering",
                        channel_output_dir=channel_output_dir,
                        events=events,
                        schedule=schedule,
                        selected=recovery_selected,
                        schedule_now=recovery_now,
                    )
                    recovery_duration_limit = min(config.duration_limit, max(0.0, (block_end - schedule_now).total_seconds()))
                    diagnostics_dict = dict(
                        self.block_runner.run(
                            BlockRunConfig(
                                channel=config.channel,
                                duration_limit=recovery_duration_limit,
                                output_dir=channel_output_dir,
                                now=recovery_now,
                                dry_run=config.dry_run,
                                output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                                hls_start_number=hls_start_number,
                                hls_start_time_offset=hls_start_time_offset if config.stream_profile == "jellyfin" else None,
                                hls_append=True,
                                hls_boundary_starts=tuple(hls_boundary_starts),
                                schedule_timezone=config.schedule_timezone,
                                stream_profile=config.stream_profile,
                            )
                        )
                    )
                    if isinstance(diagnostics_dict.get("hls_boundary_starts"), list):
                        hls_boundary_starts = [int(number) for number in diagnostics_dict["hls_boundary_starts"]]
                    hls_start_number = int(diagnostics_dict.get("hls_next_start_number") or hls_start_number)
                    hls_start_time_offset = _next_hls_start_time_offset(diagnostics_dict, fallback=hls_start_time_offset)
                    events.append(_complete_event(ordinal=ordinal, block=recovery_block_info, diagnostics=diagnostics_dict))
                    _emit_live_status(
                        config,
                        status="running",
                        channel_output_dir=channel_output_dir,
                        events=events,
                        schedule=schedule,
                        selected=recovery_selected,
                        schedule_now=recovery_now,
                    )
                    if not _block_run_failed(diagnostics_dict):
                        selected = recovery_selected
                        block_info = recovery_block_info
                        break

            last_index = selected.index
            if not simulated_cursor and block_end is not None and ordinal < config.max_blocks - 1:
                schedule_now = _schedule_now(config.clock(), config.schedule_timezone)
                wait_seconds = max(0.0, (block_end - schedule_now).total_seconds())
                if wait_seconds > 0:
                    events.append(
                        {
                            "event": "block_wait_until_boundary",
                            "block_number": ordinal + 1,
                            "block": block_info,
                            "wait_seconds": wait_seconds,
                            "until": block_end.isoformat(),
                        }
                    )
                    _emit_live_status(
                        config,
                        status="running",
                        channel_output_dir=channel_output_dir,
                        events=events,
                        schedule=schedule,
                        selected=selected,
                        schedule_now=config.clock(),
                    )
                    filler_diagnostics = dict(
                        self.filler_runner.run(
                            output_dir=channel_output_dir,
                            output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                            duration=wait_seconds,
                            hls_start_number=hls_start_number,
                            hls_start_time_offset=hls_start_time_offset if config.stream_profile == "jellyfin" else None,
                            hls_append=True,
                            stream_profile=config.stream_profile,
                        )
                    )
                    filler_playlist = Path(str(filler_diagnostics.get("playlist") or ""))
                    if filler_playlist.exists():
                        if hls_start_number not in hls_boundary_starts:
                            hls_boundary_starts.append(hls_start_number)
                            hls_boundary_starts.sort()
                        filler_state = _rewrite_live_playlist_boundaries(filler_playlist, boundary_starts=hls_boundary_starts)
                        filler_diagnostics["hls_boundary_starts"] = list(hls_boundary_starts)
                        filler_diagnostics["hls_discontinuity_sequence"] = filler_state.get("discontinuity_sequence")
                    events.append(
                        {
                            "event": "block_filler",
                            "block_number": ordinal + 1,
                            "block": block_info,
                            "duration": wait_seconds,
                            "until": block_end.isoformat(),
                            "status": filler_diagnostics.get("status"),
                            "playlist": filler_diagnostics.get("playlist"),
                            "hls_start_number": hls_start_number,
                            "hls_start_time_offset": hls_start_time_offset if config.stream_profile == "jellyfin" else None,
                            "hls_next_start_number": filler_diagnostics.get("hls_next_start_number"),
                            "hls_next_start_time_offset": filler_diagnostics.get("hls_next_start_time_offset"),
                            "ffmpeg_returncode": _ffmpeg_returncode(filler_diagnostics),
                        }
                    )
                    hls_start_number = int(filler_diagnostics.get("hls_next_start_number") or hls_start_number)
                    hls_start_time_offset = _next_hls_start_time_offset(filler_diagnostics, fallback=hls_start_time_offset)
                    _emit_live_status(
                        config,
                        status="running",
                        channel_output_dir=channel_output_dir,
                        events=events,
                        schedule=schedule,
                        selected=selected,
                        schedule_now=config.clock(),
                    )
            cursor = block_end or run_now or cursor

        summary = _events_plan_summary(events)
        result = {
            "status": "complete",
            "channel": config.channel,
            "channel_output_dir": str(channel_output_dir),
            "output_root": str(config.output_root),
            "max_blocks": config.max_blocks,
            "blocks_completed": config.max_blocks,
            "duration_limit": config.duration_limit,
            "stream_profile": config.stream_profile,
            **summary,
            "events": events,
        }
        if config.status_callback is not None:
            config.status_callback(result)
        return result


def _num(value: float) -> str:
    return (f"{value:.6f}").rstrip("0").rstrip(".")


def _count_hls_segments_from(playlist: Path, *, start_number: int) -> int:
    if not playlist.exists():
        return 0
    count = 0
    for line in playlist.read_text(errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        stem = Path(text).stem
        suffix = stem.rsplit("_", 1)[-1]
        if suffix.isdigit() and int(suffix) >= start_number:
            count += 1
    return count


def _block_run_failed(diagnostics: Mapping[str, Any]) -> bool:
    status = str(diagnostics.get("status") or "")
    if status and status not in {"ok", "dry-run"}:
        return True
    returncode = _ffmpeg_returncode(diagnostics)
    return isinstance(returncode, int) and returncode != 0


def _ffmpeg_returncode(diagnostics: Mapping[str, Any]) -> Any:
    raw_ffmpeg = diagnostics.get("ffmpeg")
    if isinstance(raw_ffmpeg, Mapping):
        return raw_ffmpeg.get("returncode")
    return None


def _next_hls_start_time_offset(diagnostics: Mapping[str, Any], *, fallback: float) -> float:
    raw_offset = diagnostics.get("hls_next_start_time_offset")
    if isinstance(raw_offset, (int, float)):
        return float(raw_offset)
    return fallback


def _emit_live_status(
    config: LiveControllerConfig,
    *,
    status: str,
    channel_output_dir: Path,
    events: Sequence[Mapping[str, Any]],
    schedule: Mapping[str, Any],
    selected: SelectedBlock,
    schedule_now: datetime | None,
) -> None:
    if config.status_callback is None:
        return
    active_block = _block_info(selected)
    raw_plan = selected.block.get("plan")
    if isinstance(raw_plan, list):
        active_block["plan"] = [dict(item) for item in raw_plan if isinstance(item, Mapping)]
    local_schedule_now = _schedule_now(schedule_now, config.schedule_timezone) if schedule_now is not None else None
    upcoming_blocks = _upcoming_blocks(schedule, after_index=selected.index)
    payload = {
        "status": status,
        "channel": config.channel,
        "channel_output_dir": str(channel_output_dir),
        "output_root": str(config.output_root),
        "max_blocks": config.max_blocks,
        "duration_limit": config.duration_limit,
        "schedule_timezone": config.schedule_timezone,
        "stream_profile": config.stream_profile,
        "schedule_now": local_schedule_now.isoformat() if local_schedule_now is not None else None,
        "active_block": active_block,
        "upcoming_blocks": upcoming_blocks,
        "hls": {
            "playlist": str(channel_output_dir / f"{FFMpegHLSCommandBuilder._slug(config.channel)}.m3u8"),
            "channel_output_dir": str(channel_output_dir),
            "output_root": str(config.output_root),
        },
        "events": [dict(event) for event in events],
    }
    config.status_callback(payload)


def _upcoming_blocks(schedule: Mapping[str, Any], *, after_index: int, limit: int = 5) -> list[dict[str, Any]]:
    blocks = schedule.get("schedule_blocks")
    if not isinstance(blocks, list):
        return []
    upcoming: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if index <= after_index or not isinstance(block, Mapping):
            continue
        upcoming.append(
            {
                "index": index,
                "selection_reason": "upcoming",
                "title": str(block.get("title") or "untitled"),
                "start_time": block.get("start_time"),
                "end_time": block.get("end_time"),
            }
        )
        if len(upcoming) >= limit:
            break
    return upcoming


def clean_hls_outputs(directory: Path) -> list[Path]:
    """Remove stale HLS playlists/segments from a stable channel directory only."""

    directory.mkdir(parents=True, exist_ok=True)
    removed: list[Path] = []
    root = directory.resolve(strict=False)
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".m3u8", ".ts"} or not path.is_file():
            continue
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:  # defensive guard against unsafe traversal/symlink surprises
            raise ValueError(f"refusing to remove HLS output outside channel directory: {path}") from exc
        path.unlink()
        removed.append(path)
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded FS42 live block lifecycle controller.")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--output-root", type=Path, default=Path("/tmp/fs42stream-live"))
    parser.add_argument("--max-blocks", type=int, default=2)
    parser.add_argument("--duration-limit", type=float, default=10.0)
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--fs42-root", type=Path, default=Path(DEFAULT_FS42_ROOT))
    parser.add_argument("--sdtv-root", type=Path, default=Path(DEFAULT_SDTV_ROOT))
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE)
    parser.add_argument("--video-encoder", default="libx264", help="video encoder, e.g. libx264 or h264_vaapi")
    parser.add_argument("--vaapi-device", help="VAAPI device path, e.g. /dev/dri/renderD128")
    parser.add_argument("--schedule-timezone", default=DEFAULT_SCHEDULE_TIMEZONE, help="timezone for naive FS42 schedule timestamps, e.g. Europe/London")
    parser.add_argument("--max-recovery-attempts-per-block", type=int, default=1, help="bounded ffmpeg failure recovery attempts within a schedule block")
    parser.add_argument("--fallback-slate-video", type=Path)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--now", help="override current time for deterministic tests, e.g. 2026-06-17T10:05:00")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stream-profile", choices=("direct", "jellyfin"), default="direct", help="HLS packaging profile; jellyfin writes under <channel>/jellyfin")
    args = parser.parse_args(argv)

    schedule_client = FS42ScheduleClient(args.api_base_url, timeout=args.timeout)
    block_runner = BlockRunner(
        client=FS42ScheduleClient(args.api_base_url, timeout=args.timeout),
        planner=BlockPlanner(PathResolver(fs42_root=args.fs42_root, sdtv_root=args.sdtv_root), FFProbe(args.ffprobe), fallback_slate_video=args.fallback_slate_video),
        builder=FFMpegHLSCommandBuilder(args.ffmpeg, video_encoder=args.video_encoder, vaapi_device=args.vaapi_device),
    )
    controller = LiveController(schedule_client=schedule_client, block_runner=block_runner)
    config = LiveControllerConfig(
        channel=args.channel,
        output_root=args.output_root,
        max_blocks=args.max_blocks,
        duration_limit=args.duration_limit,
        now=_parse_datetime(args.now) if args.now else None,
        dry_run=args.dry_run,
        schedule_timezone=args.schedule_timezone,
        max_recovery_attempts_per_block=args.max_recovery_attempts_per_block,
        stream_profile=args.stream_profile,
    )
    try:
        result = controller.run(config)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _select_not_before(schedule: Mapping[str, Any], *, now: datetime | None, minimum_index: int, schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE) -> SelectedBlock:
    selected = select_current_or_next_block(schedule, now=now, schedule_timezone=schedule_timezone)
    if selected.index >= minimum_index:
        return selected
    blocks = schedule.get("schedule_blocks")
    if not isinstance(blocks, list):
        raise ValueError("schedule contains no schedule_blocks")
    for index in range(minimum_index, len(blocks)):
        block = blocks[index]
        if not isinstance(block, Mapping):
            raise ValueError(f"schedule block {index} is not a JSON object")
        return SelectedBlock(index=index, reason="next", block=block)
    return selected


def _block_info(selected: SelectedBlock) -> dict[str, Any]:
    return {
        "index": selected.index,
        "selection_reason": selected.reason,
        "title": str(selected.block.get("title") or "untitled"),
        "start_time": selected.block.get("start_time"),
        "end_time": selected.block.get("end_time"),
    }


def _complete_event(*, ordinal: int, block: Mapping[str, Any], diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    raw_ffmpeg = diagnostics.get("ffmpeg")
    raw_hls = diagnostics.get("hls")
    ffmpeg: Mapping[str, Any] = raw_ffmpeg if isinstance(raw_ffmpeg, Mapping) else {}
    hls: Mapping[str, Any] = raw_hls if isinstance(raw_hls, Mapping) else {}
    playlist = hls.get("playlist") or diagnostics.get("playlist")
    segments = hls.get("segments") or []
    event = {
        "event": "block_complete",
        "block_number": ordinal + 1,
        "block": dict(block),
        "status": diagnostics.get("status"),
        "playlist": playlist,
        "playlist_paths": {"playlist": playlist, "segments": segments},
        "ffmpeg_returncode": ffmpeg.get("returncode"),
        "runtime_fallback_diagnostics": _runtime_fallback_diagnostics(diagnostics),
        "catch_up": diagnostics.get("catch_up"),
    }
    for key in ("plan_item_count", "plan_item_counts", "commercial_count", "ad_count", "commercial_paths", "commercial_path_count"):
        if key in diagnostics:
            event[key] = diagnostics[key]
    return event


def _runtime_fallback_diagnostics(diagnostics: Mapping[str, Any]) -> list[str]:
    plan = diagnostics.get("plan")
    if not isinstance(plan, list):
        return []
    messages: list[str] = []
    for item in plan:
        if not isinstance(item, Mapping):
            continue
        if item.get("runtime_action"):
            diagnostic = item.get("diagnostic") or item.get("runtime_action")
            messages.append(str(diagnostic))
    return messages


def _events_plan_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    total_items = 0
    commercial_count = 0
    commercial_paths: list[str] = []
    for event in events:
        if event.get("event") != "block_complete":
            continue
        raw_count = event.get("plan_item_count")
        if isinstance(raw_count, int):
            total_items += raw_count
        raw_counts = event.get("plan_item_counts")
        if isinstance(raw_counts, Mapping):
            for key, value in raw_counts.items():
                if isinstance(value, int):
                    item_type = str(key)
                    counts[item_type] = counts.get(item_type, 0) + value
        raw_commercial_count = event.get("commercial_count")
        if isinstance(raw_commercial_count, int):
            commercial_count += raw_commercial_count
        raw_paths = event.get("commercial_paths")
        if isinstance(raw_paths, list):
            commercial_paths.extend(str(path) for path in raw_paths)
    return {
        "plan_item_count": total_items,
        "plan_item_counts": counts,
        "commercial_count": commercial_count,
        "ad_count": commercial_count,
        "commercial_paths": commercial_paths,
        "commercial_path_count": len(commercial_paths),
    }


if __name__ == "__main__":
    raise SystemExit(main())
