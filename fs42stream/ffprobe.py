from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProbeResult:
    duration: float
    width: int
    height: int
    fps: float
    audio_sample_rate: int
    audio_channels: int


class FFProbe:
    """ffprobe JSON helper for validating video inputs before planning HLS."""

    def __init__(self, executable: str = "/usr/bin/ffprobe") -> None:
        self.executable = executable

    def validate_video(self, path: Path) -> ProbeResult:
        cmd = [
            self.executable,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-print_format",
            "json",
            str(path),
        ]
        completed = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if completed.returncode != 0:
            raise ValueError(f"ffprobe failed for {path}: {completed.stderr.strip()}")
        try:
            payload: dict[str, Any] = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ffprobe returned invalid JSON for {path}") from exc
        streams = payload.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not video or not audio:
            raise ValueError(f"ffprobe did not find both video and audio streams for {path}")
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
        return ProbeResult(
            duration=duration,
            width=int(video.get("width") or 0),
            height=int(video.get("height") or 0),
            fps=self._parse_fps(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")),
            audio_sample_rate=int(audio.get("sample_rate") or 0),
            audio_channels=int(audio.get("channels") or 0),
        )

    @staticmethod
    def _parse_fps(value: str) -> float:
        try:
            return float(Fraction(value))
        except Exception:
            return 0.0
