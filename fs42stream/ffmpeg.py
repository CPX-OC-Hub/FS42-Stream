from __future__ import annotations

import re
from pathlib import Path

from .planner import PlannedBlock


class FFMpegHLSCommandBuilder:
    """Build a one-shot block-level normalized concat-filter HLS command."""

    def __init__(self, ffmpeg: str = "/usr/bin/ffmpeg") -> None:
        self.ffmpeg = ffmpeg

    def build(self, block: PlannedBlock, *, output_dir: Path, duration_limit: float | None = None) -> list[str]:
        if not block.items:
            raise ValueError("cannot build ffmpeg command for an empty block")
        if duration_limit is not None and duration_limit <= 0:
            raise ValueError("duration_limit must be positive")

        cmd: list[str] = [self.ffmpeg, "-hide_banner", "-y"]
        for item in block.items:
            if item.input_kind == "lavfi":
                if item.duration > 0:
                    cmd.extend(["-t", self._num(item.duration)])
                cmd.extend(["-f", "lavfi", "-i", item.ffmpeg_input or "color=black"])
                continue
            if item.skip > 0:
                cmd.extend(["-ss", self._num(item.skip)])
            if item.duration > 0:
                cmd.extend(["-t", self._num(item.duration)])
            cmd.extend(["-i", item.ffmpeg_input or str(item.resolved_path)])

        filter_complex = self._filter_complex(block)
        playlist = output_dir / f"{self._slug(block.title)}.m3u8"
        segment_pattern = output_dir / f"{self._slug(block.title)}_%05d.ts"
        cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
        )
        if duration_limit is not None:
            cmd.extend(["-t", self._num(duration_limit)])
        cmd.extend(
            [
                "-f",
                "hls",
                "-hls_time",
                "6",
                "-hls_playlist_type",
                "event",
                "-hls_segment_filename",
                str(segment_pattern),
                str(playlist),
            ]
        )
        return cmd

    @staticmethod
    def _filter_complex(block: PlannedBlock) -> str:
        chains: list[str] = []
        labels: list[str] = []
        for idx, item in enumerate(block.items):
            chains.append(
                f"[{idx}:v]scale=640:480:force_original_aspect_ratio=decrease,"
                "pad=640:480:(ow-iw)/2:(oh-ih)/2,"
                "fps=25,setsar=1,format=yuv420p"
                f"[v{idx}]"
            )
            if item.probe.audio_channels > 0:
                chains.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo[a{idx}]")
            else:
                duration = item.duration or item.probe.duration or 1.0
                chains.append(
                    "anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=duration={FFMpegHLSCommandBuilder._num(duration)},"
                    "asetpts=PTS-STARTPTS"
                    f"[a{idx}]"
                )
            labels.append(f"[v{idx}][a{idx}]")
        chains.append("".join(labels) + f"concat=n={len(block.items)}:v=1:a=1[vout][aout]")
        return ";".join(chains)

    @staticmethod
    def _slug(title: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
        return slug or "block"

    @staticmethod
    def _num(value: float) -> str:
        return (f"{value:.6f}").rstrip("0").rstrip(".")
