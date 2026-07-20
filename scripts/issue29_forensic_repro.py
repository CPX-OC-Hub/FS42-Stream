from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import FFProbe
from fs42stream.hls_harness import create_fixture_clips
from fs42stream.live_controller import HLSBlackSlateFillerRunner, LiveController, LiveControllerConfig
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner
from fs42stream.run_block import BlockRunner


class FixtureScheduleClient:
    def __init__(self, schedule: dict):
        self.schedule = schedule

    def fetch_schedule(self, channel: str, expected_blocks: int | None = None) -> dict:
        return self.schedule


def main() -> int:
    ffmpeg = "/usr/bin/ffmpeg"
    ffprobe = "/usr/bin/ffprobe"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clips = create_fixture_clips(root / "clips", count=8, duration=2.2, ffmpeg=ffmpeg)
        blocks: list[dict] = []
        base = datetime(2026, 6, 17, 10, 0, 0)
        for index in range(4):
            start = base + timedelta(seconds=index * 8)
            end = start + timedelta(seconds=4)
            blocks.append(
                {
                    "title": f"Block {index}",
                    "start_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "end_time": end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "plan": [
                        {
                            "realpath": str(clips[index * 2]),
                            "duration": 2.2,
                            "skip": 0,
                            "is_stream": False,
                            "content_type": "feature",
                            "media_type": "video",
                        },
                        {
                            "realpath": str(clips[index * 2 + 1]),
                            "duration": 1.1,
                            "skip": 0,
                            "is_stream": False,
                            "content_type": "commercial",
                            "media_type": "video",
                        },
                    ],
                }
            )
        schedule = {"network_name": "Sky One", "schedule_blocks": blocks}
        clock_values: list[datetime] = []
        for index in range(4):
            start = base + timedelta(seconds=index * 8)
            end = start + timedelta(seconds=4)
            next_start = base + timedelta(seconds=(index + 1) * 8) if index < 3 else end
            clock_values.extend([start, end, next_start])

        def clock() -> datetime:
            return clock_values.pop(0) if clock_values else base + timedelta(seconds=32)

        client = FixtureScheduleClient(schedule)
        runner = BlockRunner(
            client=client,  # type: ignore[arg-type]
            planner=BlockPlanner(PathResolver(fs42_root=root, sdtv_root=root), FFProbe(ffprobe)),
            builder=FFMpegHLSCommandBuilder(ffmpeg),
        )
        controller = LiveController(
            schedule_client=client,
            block_runner=runner,
            filler_runner=HLSBlackSlateFillerRunner(ffmpeg),
        )
        result = controller.run(
            LiveControllerConfig(
                channel="Sky One",
                output_root=root / "out",
                max_blocks=4,
                duration_limit=7200,
                clock=clock,
                stream_profile="jellyfin",
            )
        )
        playlist = Path(result["channel_output_dir"]) / "Sky_One.m3u8"
        playlist_text = playlist.read_text()
        source_segments = [line.strip() for line in playlist_text.splitlines() if line and not line.startswith("#")]
        remux_playlist = root / "remux.m3u8"
        remux_cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-analyzeduration",
            "3000000",
            "-probesize",
            "100M",
            "-fflags",
            "+igndts+genpts",
            "-f",
            "hls",
            "-i",
            str(playlist),
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-codec:v:0",
            "copy",
            "-start_at_zero",
            "-flags",
            "-global_header",
            "-codec:a:0",
            "copy",
            "-copyts",
            "-avoid_negative_ts",
            "disabled",
            "-max_muxing_queue_size",
            "2048",
            "-f",
            "hls",
            "-hls_time",
            "3",
            "-hls_segment_type",
            "mpegts",
            "-start_number",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_list_size",
            "0",
            "-y",
            str(remux_playlist),
        ]
        remux = subprocess.run(remux_cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        warning_lines = [
            line
            for line in remux.stderr.splitlines()
            if "corrupt" in line.lower() or "non-monotonic" in line.lower()
        ]
        segment_probe: dict[str, dict[str, str]] = {}
        for segment_name in source_segments:
            probe = subprocess.check_output(
                [ffprobe, "-v", "error", "-show_entries", "format=start_time,duration", "-of", "json", str(playlist.parent / segment_name)],
                text=True,
            )
            segment_probe[segment_name] = json.loads(probe).get("format", {})
        payload = {
            "playlist": str(playlist),
            "playlist_text": playlist_text,
            "discontinuity_count": playlist_text.count("#EXT-X-DISCONTINUITY"),
            "source_segments": source_segments,
            "segment_probe": segment_probe,
            "remux_returncode": remux.returncode,
            "remux_warning_count": len(warning_lines),
            "remux_warning_excerpt": warning_lines[:20],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
