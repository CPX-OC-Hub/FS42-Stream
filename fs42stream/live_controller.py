from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .client import FS42ScheduleClient
from .ffmpeg import FFMpegHLSCommandBuilder
from .ffprobe import FFProbe
from .paths import PathResolver
from .planner import BlockPlanner
from .run_block import (
    DEFAULT_API_BASE_URL,
    DEFAULT_CHANNEL,
    DEFAULT_FFMPEG,
    DEFAULT_FFPROBE,
    DEFAULT_FS42_ROOT,
    DEFAULT_SDTV_ROOT,
    BlockRunConfig,
    BlockRunner,
    SelectedBlock,
    _parse_datetime,
    select_current_or_next_block,
)


@dataclass(frozen=True)
class LiveControllerConfig:
    channel: str = DEFAULT_CHANNEL
    output_root: Path = Path("/tmp/fs42stream-live")
    max_blocks: int = 2
    duration_limit: float = 10.0
    now: datetime | None = None
    dry_run: bool = False
    status_callback: Callable[[Mapping[str, Any]], None] | None = None


class ScheduleClient(Protocol):
    def fetch_schedule(self, channel: str, *, expected_blocks: int | None = None) -> Mapping[str, Any]: ...


class SingleBlockRunner(Protocol):
    def run(self, config: BlockRunConfig) -> Mapping[str, Any]: ...


class LiveController:
    """Bounded live block lifecycle controller for stable per-channel HLS output."""

    def __init__(self, *, schedule_client: ScheduleClient | None = None, block_runner: SingleBlockRunner | None = None) -> None:
        self.schedule_client = schedule_client or FS42ScheduleClient(DEFAULT_API_BASE_URL)
        self.block_runner = block_runner or BlockRunner()

    def run(self, config: LiveControllerConfig) -> dict[str, Any]:
        if config.max_blocks <= 0:
            raise ValueError("max_blocks must be positive")
        if config.duration_limit <= 0:
            raise ValueError("duration_limit must be positive")

        channel_output_dir = config.output_root / FFMpegHLSCommandBuilder._slug(config.channel)
        channel_output_dir.mkdir(parents=True, exist_ok=True)

        events: list[dict[str, Any]] = []
        cursor = config.now
        last_index = -1

        hls_start_number = 0
        for ordinal in range(config.max_blocks):
            schedule = self.schedule_client.fetch_schedule(config.channel, expected_blocks=None)
            selected = _select_not_before(schedule, now=cursor, minimum_index=last_index + 1)
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
            )

            block_now = _parse_datetime(selected.block.get("start_time")) or cursor
            block_hls_append = ordinal > 0
            block_hls_start_number = 0 if block_hls_append else hls_start_number
            diagnostics = self.block_runner.run(
                BlockRunConfig(
                    channel=config.channel,
                    duration_limit=config.duration_limit,
                    output_dir=channel_output_dir,
                    now=block_now,
                    dry_run=config.dry_run,
                    output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                    hls_start_number=block_hls_start_number,
                    hls_append=block_hls_append,
                )
            )
            diagnostics_dict = dict(diagnostics)
            hls_start_number = int(diagnostics_dict.get("hls_next_start_number") or hls_start_number)
            events.append(_complete_event(ordinal=ordinal, block=block_info, diagnostics=diagnostics_dict))
            _emit_live_status(
                config,
                status="running",
                channel_output_dir=channel_output_dir,
                events=events,
                schedule=schedule,
                selected=selected,
            )

            last_index = selected.index
            cursor = _parse_datetime(selected.block.get("end_time")) or block_now or cursor

        summary = _events_plan_summary(events)
        result = {
            "status": "complete",
            "channel": config.channel,
            "channel_output_dir": str(channel_output_dir),
            "output_root": str(config.output_root),
            "max_blocks": config.max_blocks,
            "blocks_completed": config.max_blocks,
            "duration_limit": config.duration_limit,
            **summary,
            "events": events,
        }
        if config.status_callback is not None:
            config.status_callback(result)
        return result


def _emit_live_status(
    config: LiveControllerConfig,
    *,
    status: str,
    channel_output_dir: Path,
    events: Sequence[Mapping[str, Any]],
    schedule: Mapping[str, Any],
    selected: SelectedBlock,
) -> None:
    if config.status_callback is None:
        return
    active_block = _block_info(selected)
    upcoming_blocks = _upcoming_blocks(schedule, after_index=selected.index)
    payload = {
        "status": status,
        "channel": config.channel,
        "channel_output_dir": str(channel_output_dir),
        "output_root": str(config.output_root),
        "max_blocks": config.max_blocks,
        "duration_limit": config.duration_limit,
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
    parser.add_argument("--fallback-slate-video", type=Path)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--now", help="override current time for deterministic tests, e.g. 2026-06-17T10:05:00")
    parser.add_argument("--dry-run", action="store_true")
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
    )
    try:
        result = controller.run(config)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _select_not_before(schedule: Mapping[str, Any], *, now: datetime | None, minimum_index: int) -> SelectedBlock:
    selected = select_current_or_next_block(schedule, now=now)
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
