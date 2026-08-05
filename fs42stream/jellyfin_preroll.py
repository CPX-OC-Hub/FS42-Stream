"""Private staged HLS publication for Jellyfin schedule boundaries.

The staging playlist is never served by the public HLS directory.  At the
scheduled boundary, ready staged segments are copied into the stable public
channel directory under the next monotonic segment numbers and atomically
advertised in the public playlist.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StagedRunTiming:
    """Wall-clock timing used when creating a private pre-roll encode."""

    run_now: datetime
    media_offset: float


def staged_run_timing(*, block_start: datetime, stage_started_at: datetime) -> StagedRunTiming:
    """Use media zero before a boundary and normal schedule catch-up afterwards."""
    if stage_started_at <= block_start:
        return StagedRunTiming(run_now=block_start, media_offset=0.0)
    return StagedRunTiming(
        run_now=stage_started_at,
        media_offset=max(0.0, (stage_started_at - block_start).total_seconds()),
    )


class GatedJellyfinPreRoll:
    """Publish a private staged playlist only at/after its scheduled boundary."""

    def __init__(
        self,
        *,
        public_playlist: Path,
        staged_playlist: Path,
        publish_at: datetime,
        public_next_segment_number: int,
    ) -> None:
        if public_next_segment_number < 0:
            raise ValueError("public_next_segment_number must be non-negative")
        self.public_playlist = public_playlist
        self.staged_playlist = staged_playlist
        self.publish_at = publish_at
        self.public_next_segment_number = public_next_segment_number
        self._published_source_names: set[str] = set()
        self._boundary_event: dict[str, Any] | None = None

    @property
    def staged_block(self) -> str:
        return self.staged_playlist.parent.name

    def status(self, *, now: datetime, fallback_reason: str | None = None) -> dict[str, Any]:
        ready = len(_playlist_entries(self.staged_playlist))
        if fallback_reason:
            state = "fallback"
        elif self._boundary_event:
            state = "published"
        elif now < self.publish_at:
            state = "staged-private"
        else:
            state = "awaiting-staged-segments"
        payload: dict[str, Any] = {
            "pre_roll_state": state,
            "staged_block": self.staged_block,
            "staged_segments_ready": ready,
            "publish_at": self.publish_at.isoformat(),
        }
        if self._boundary_event is not None:
            payload["public_boundary_switch"] = dict(self._boundary_event)
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason
        return payload

    def publish_if_due(self, now: datetime) -> dict[str, Any]:
        """Synchronize ready staged media only once the schedule permits it."""
        if now < self.publish_at:
            return self.status(now=now)
        entries = _playlist_entries(self.staged_playlist)
        if not entries:
            return self.status(now=now, fallback_reason="no-staged-segments-ready")

        appended = 0
        public_entries = _playlist_entries(self.public_playlist)
        for duration, source_name in entries:
            if source_name in self._published_source_names:
                continue
            source = self.staged_playlist.parent / source_name
            if not source.is_file():
                continue
            target_name = f"{self.public_playlist.stem}_{self.public_next_segment_number:05d}.ts"
            target = self.public_playlist.parent / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            public_entries.append((duration, target_name))
            self._published_source_names.add(source_name)
            self.public_next_segment_number += 1
            appended += 1

        if appended == 0 and self._boundary_event is None:
            return self.status(now=now, fallback_reason="no-staged-segment-files-ready")
        self._write_public_playlist(public_entries)
        if self._boundary_event is None:
            self._boundary_event = {
                "event": "public_boundary_switch",
                "at": now.isoformat(),
                "publish_at": self.publish_at.isoformat(),
                "first_public_segment_number": self.public_next_segment_number - appended,
                "segments_published": appended,
            }
        return self.status(now=now)

    def _write_public_playlist(self, entries: list[tuple[float, str]]) -> None:
        # Never carry ENDLIST or discontinuity tags through an internal boundary.
        first_number = _segment_number(entries[0][1]) if entries else self.public_next_segment_number
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:2",
            f"#EXT-X-MEDIA-SEQUENCE:{first_number}",
        ]
        for duration, name in entries:
            lines.extend([f"#EXTINF:{duration:.6f},", name])
        tmp = self.public_playlist.with_name(f".{self.public_playlist.name}.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(self.public_playlist)


def _playlist_entries(playlist: Path) -> list[tuple[float, str]]:
    try:
        lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[tuple[float, str]] = []
    duration: float | None = None
    for line in lines:
        text = line.strip()
        if text.startswith("#EXTINF:"):
            try:
                duration = float(text.removeprefix("#EXTINF:").split(",", 1)[0])
            except ValueError:
                duration = None
        elif text and not text.startswith("#"):
            if duration is not None:
                entries.append((duration, text))
            duration = None
    return entries


def _segment_number(name: str) -> int:
    suffix = Path(name).stem.rsplit("_", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0
