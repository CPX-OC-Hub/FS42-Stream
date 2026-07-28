from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ffmpeg import FFMpegHLSCommandBuilder
from .ffprobe import FFProbe
from .paths import PathResolver
from .planner import BlockPlanner


DEFAULT_FFMPEG = "/usr/bin/ffmpeg"
DEFAULT_FFPROBE = "/usr/bin/ffprobe"


@dataclass(frozen=True)
class HLSInspection:
    playlist: Path
    segments: list[Path]
    has_endlist: bool


def create_fixture_clips(
    output_dir: Path,
    *,
    count: int = 3,
    duration: float = 1.0,
    ffmpeg: str = DEFAULT_FFMPEG,
) -> list[Path]:
    """Create deterministic synthetic MP4 clips with ffmpeg lavfi sources.

    The clips are deliberately tiny and generated in the caller's temp/work dir so
    the harness never depends on production media. ffmpeg is always executed via
    an argv list with shell=False.
    """

    if count < 1:
        raise ValueError("count must be at least 1")
    if duration <= 0:
        raise ValueError("duration must be positive")

    output_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    for index in range(count):
        path = output_dir / f"clip_{index:02d}.mp4"
        frequency = 440 + (index * 110)
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={_num(duration)}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=44100:duration={_num(duration)}",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "1",
            str(path),
        ]
        subprocess.run(cmd, check=True, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        clips.append(path)
    return clips


def fixture_schedule(clips: Sequence[Path], *, duration: float = 1.0, channel_name: str = "Example Channel") -> dict[str, Any]:
    """Build a portable test fixture whose plan references fixture realpaths."""

    if not clips:
        raise ValueError("at least one fixture clip is required")
    return {
        "network_name": channel_name,
        "schedule_blocks": [
            {
                "title": f"{channel_name} HLS Harness",
                "start_time": "2026-06-17T00:00:00",
                "end_time": "2026-06-17T00:00:03",
                "plan": [
                    {
                        "realpath": str(path),
                        "skip": 0,
                        "duration": duration,
                        "is_stream": False,
                        "content_type": "fixture",
                        "media_type": "video",
                    }
                    for path in clips
                ],
            }
        ],
    }


class HLSHarness:
    """Small one-shot integration harness for fixture-backed HLS generation."""

    def __init__(
        self,
        *,
        planner: BlockPlanner | None = None,
        builder: FFMpegHLSCommandBuilder | None = None,
    ) -> None:
        self.planner = planner or BlockPlanner(probe=FFProbe(DEFAULT_FFPROBE))
        self.builder = builder or FFMpegHLSCommandBuilder(DEFAULT_FFMPEG)

    def run(self, schedule: Mapping[str, Any], *, output_dir: Path, dry_run: bool = False) -> list[str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        blocks = self.planner.plan(schedule)
        if not blocks:
            raise ValueError("schedule contains no blocks")
        command = self.builder.build(blocks[0], output_dir=output_dir)
        if dry_run:
            return command
        subprocess.run(command, check=True, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return command


def inspect_hls_output(playlist: Path) -> HLSInspection:
    """Inspect a completed media playlist and return referenced local segments."""

    if not playlist.exists():
        raise FileNotFoundError(playlist)
    lines = playlist.read_text(encoding="utf-8").splitlines()
    segment_paths = [
        (playlist.parent / line).resolve(strict=False)
        for line in lines
        if line and not line.startswith("#")
    ]
    missing = [path for path in segment_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"playlist references missing segments: {missing}")
    return HLSInspection(
        playlist=playlist,
        segments=segment_paths,
        has_endlist="#EXT-X-ENDLIST" in lines,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate fixture-backed HLS for FS42-Stream phase 1.")
    parser.add_argument("--work-dir", type=Path, default=Path("build/hls-harness"), help="directory for fixtures and HLS output")
    parser.add_argument("--count", type=int, default=3, help="number of synthetic fixture clips to create")
    parser.add_argument("--duration", type=float, default=1.0, help="duration of each synthetic clip in seconds")
    parser.add_argument("--dry-run", action="store_true", help="print ffmpeg HLS argv without running it")
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE)
    parser.add_argument("--channel", default="Example Channel", help="fixture channel name used for generated schedule metadata")
    args = parser.parse_args(argv)

    clips = create_fixture_clips(args.work_dir / "fixtures", count=args.count, duration=args.duration, ffmpeg=args.ffmpeg)
    schedule = fixture_schedule(clips, duration=args.duration, channel_name=args.channel)
    harness = HLSHarness(
        planner=BlockPlanner(PathResolver(fs42_root=args.work_dir, sdtv_root=args.work_dir), FFProbe(args.ffprobe)),
        builder=FFMpegHLSCommandBuilder(args.ffmpeg),
    )
    output_dir = args.work_dir / "hls"
    command = harness.run(schedule, output_dir=output_dir, dry_run=args.dry_run)
    print(" ".join(command))
    if not args.dry_run:
        playlist_name = f"{_slug(args.channel)}_HLS_Harness.m3u8"
        inspection = inspect_hls_output(output_dir / playlist_name)
        print(f"playlist={inspection.playlist}")
        print(f"segments={len(inspection.segments)}")
        print(f"endlist={inspection.has_endlist}")
    return 0


def _num(value: float) -> str:
    return (f"{value:.6f}").rstrip("0").rstrip(".")


def _slug(value: str) -> str:
    return "_".join(part for part in value.split() if part) or "Example_Channel"


if __name__ == "__main__":
    raise SystemExit(main())
