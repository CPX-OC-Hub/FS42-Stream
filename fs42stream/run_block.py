from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .client import FS42ScheduleClient
from .ffmpeg import FFMpegHLSCommandBuilder
from .ffprobe import FFProbe
from .hls_harness import inspect_hls_output
from .paths import PathResolver
from .planner import BlockPlanner, PlannedBlock

DEFAULT_API_BASE_URL = "http://192.168.10.252:4242"
DEFAULT_CHANNEL = "Sky One"
DEFAULT_FS42_ROOT = "/mnt/fs42"
DEFAULT_SDTV_ROOT = "/mnt/media/SDTV"
DEFAULT_FFMPEG = "/usr/bin/ffmpeg"
DEFAULT_FFPROBE = "/usr/bin/ffprobe"


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


def select_current_or_next_block(schedule: Mapping[str, Any], *, now: datetime | None = None) -> SelectedBlock:
    """Return the current FS42 schedule block, or the next future block.

    Selection is deterministic: schedule order is preserved for tie-breaking, the
    first block where ``start_time <= now < end_time`` wins, and otherwise the
    future block with the earliest start time wins. If all blocks are in the
    past, the last block is selected with reason ``last`` so callers receive a
    clear diagnostic rather than a hidden fallback.
    """

    current_time = now or datetime.now()
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
        config.output_dir.mkdir(parents=True, exist_ok=True)

        schedule = self.client.fetch_schedule(config.channel, expected_blocks=None)
        selected = select_current_or_next_block(schedule, now=config.now)
        planned = self.planner.plan_block(selected.block)
        command = self.builder.build(planned, output_dir=config.output_dir, duration_limit=config.duration_limit)

        diagnostics = _diagnostics(
            status="dry-run" if config.dry_run else "ok",
            channel=config.channel,
            schedule=schedule,
            selected=selected,
            planned=planned,
            output_dir=config.output_dir,
            duration_limit=config.duration_limit,
            command=command,
        )
        if config.dry_run:
            return diagnostics

        completed = subprocess.run(command, check=False, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        diagnostics["status"] = "ok" if completed.returncode == 0 else "ffmpeg-error"
        diagnostics["ffmpeg"] = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
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
    parser.add_argument("--fallback-slate-video", type=Path, help="optional prebuilt video used instead of generated black slate for runtime/off-air image entries")
    parser.add_argument("--timeout", type=float, default=10.0, help="FS42 API timeout in seconds")
    parser.add_argument("--now", help="override current time for deterministic tests, e.g. 2026-06-17T10:05:00")
    parser.add_argument("--dry-run", action="store_true", help="validate and print command without running ffmpeg")
    args = parser.parse_args(argv)

    runner = BlockRunner(
        client=FS42ScheduleClient(args.api_base_url, timeout=args.timeout),
        planner=BlockPlanner(PathResolver(fs42_root=args.fs42_root, sdtv_root=args.sdtv_root), FFProbe(args.ffprobe), fallback_slate_video=args.fallback_slate_video),
        builder=FFMpegHLSCommandBuilder(args.ffmpeg),
    )
    config = BlockRunConfig(
        channel=args.channel,
        duration_limit=args.duration_limit,
        output_dir=args.output_dir,
        now=_parse_datetime(args.now) if args.now else None,
        dry_run=args.dry_run,
    )
    try:
        diagnostics = runner.run(config)
    except Exception as exc:
        error = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
        print(json.dumps(error, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(diagnostics, indent=2, sort_keys=True))
    return 0


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
) -> dict[str, Any]:
    playlist = output_dir / f"{FFMpegHLSCommandBuilder._slug(planned.title)}.m3u8"
    return {
        "status": status,
        "channel": channel,
        "network_name": schedule.get("network_name"),
        "duration_limit": duration_limit,
        "output_dir": str(output_dir),
        "playlist": str(playlist),
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


def _coerce_now_for(parsed: Sequence[tuple[int, Mapping[str, Any], datetime | None, datetime | None]], now: datetime) -> datetime:
    has_aware = any((start and start.tzinfo) or (end and end.tzinfo) for _idx, _block, start, end in parsed)
    if has_aware and now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    if not has_aware and now.tzinfo is not None:
        return now.replace(tzinfo=None)
    return now


if __name__ == "__main__":
    raise SystemExit(main())
