from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ffprobe import FFProbe, ProbeResult
from .paths import PathResolver


@dataclass(frozen=True)
class PlannedItem:
    """One preserved `schedule_blocks[*].plan[*]` entry plus derived metadata."""

    source: Mapping[str, Any]
    resolved_path: Path
    skip: float
    duration: float
    probe: ProbeResult
    input_kind: str = "file"
    ffmpeg_input: str | None = None
    runtime_action: str | None = None
    diagnostic: str | None = None

    @property
    def fs42_type(self) -> str:
        """Return FS42's explicit plan-item type, with legacy fallbacks."""

        return fs42_item_type(self.source)


@dataclass(frozen=True)
class PlannedBlock:
    title: str
    start_time: str | None
    end_time: str | None
    source: Mapping[str, Any]
    items: Sequence[PlannedItem]


class BlockPlanner:
    """Build block-level plans without flattening or rewriting FS42's own plan entries."""

    def __init__(self, resolver: PathResolver | None = None, probe: FFProbe | None = None, *, fallback_slate_video: str | Path | None = None) -> None:
        self.resolver = resolver or PathResolver()
        self.probe = probe or FFProbe()
        self.fallback_slate_video = Path(fallback_slate_video) if fallback_slate_video is not None else None

    def plan(self, schedule: Mapping[str, Any]) -> list[PlannedBlock]:
        raw_blocks = schedule.get("schedule_blocks")
        if not isinstance(raw_blocks, list):
            raise ValueError("schedule missing schedule_blocks list")
        return [self._plan_block(block) for block in raw_blocks]

    def plan_block(self, block: Mapping[str, Any]) -> PlannedBlock:
        """Plan a single schedule block, preserving its `plan[*]` order.

        The live block runner intentionally plans only the selected current/next
        block so a 338-block FS42 schedule does not trigger hundreds of ffprobe
        calls before starting output.
        """

        return self._plan_block(block)

    def _plan_block(self, block: Mapping[str, Any]) -> PlannedBlock:
        raw_plan = block.get("plan")
        if not isinstance(raw_plan, list):
            raise ValueError(f"schedule block {block.get('title')!r} missing plan list")
        items = [self._plan_item(item) for item in raw_plan]
        return PlannedBlock(
            title=str(block.get("title") or "untitled"),
            start_time=block.get("start_time"),
            end_time=block.get("end_time"),
            source=block,
            items=items,
        )

    def _plan_item(self, item: Mapping[str, Any]) -> PlannedItem:
        if item.get("is_stream"):
            raise ValueError("Phase 1 only supports file-backed schedule plan entries, not streams")
        raw_path = item.get("realpath") or item.get("path")
        if raw_path is None:
            raise ValueError("plan item missing path/realpath")
        resolved = self.resolver.resolve(raw_path)
        duration = float(item.get("duration") or 0.0)
        if self._is_known_runtime_off_air_slate(item, resolved):
            if self.fallback_slate_video is not None:
                return PlannedItem(
                    source=item,
                    resolved_path=resolved,
                    skip=0.0,
                    duration=duration,
                    probe=self.probe.validate_video(self.fallback_slate_video),
                    input_kind="file",
                    ffmpeg_input=str(self.fallback_slate_video),
                    runtime_action="configured_fallback_slate",
                    diagnostic="known runtime/off-air image slate replaced with configured fallback video",
                )
            return PlannedItem(
                source=item,
                resolved_path=resolved,
                skip=0.0,
                duration=duration,
                probe=ProbeResult(duration=duration, width=640, height=480, fps=25.0, audio_sample_rate=0, audio_channels=0),
                input_kind="lavfi",
                ffmpeg_input="color=black",
                runtime_action="generated_fallback_slate",
                diagnostic="known runtime/off-air image slate replaced with generated fallback video",
            )
        skip = float(item.get("skip") or 0.0)
        probe = self.probe.validate_video(resolved)
        available_duration = max(0.0, float(probe.duration or 0.0) - skip)
        effective_duration = duration
        if available_duration > 0:
            effective_duration = min(duration, available_duration) if duration > 0 else available_duration
        return PlannedItem(
            source=item,
            resolved_path=resolved,
            skip=skip,
            duration=effective_duration,
            probe=probe,
        )

    def _is_known_runtime_off_air_slate(self, item: Mapping[str, Any], resolved: Path) -> bool:
        try:
            resolved.relative_to(self.resolver.fs42_root.resolve(strict=False) / "runtime")
            under_runtime = True
        except ValueError:
            under_runtime = False
        if not under_runtime:
            return False
        media_type = str(item.get("media_type") or "").lower()
        content_type = str(item.get("content_type") or "").lower()
        suffix = resolved.suffix.lower()
        return media_type in {"image", "slate"} or content_type in {"slate", "off-air", "off_air", "brb"} or suffix in {".png", ".jpg", ".jpeg"}


def fs42_item_type(item: Mapping[str, Any]) -> str:
    """Return a deterministic label for an FS42 plan item type.

    Live FS42 schedules use an explicit ``type`` field (for example
    ``commercial``). Older fixtures in this project used ``content_type``;
    falling back keeps diagnostics useful without rewriting source entries.
    """

    for key in ("type", "content_type", "media_type"):
        value = item.get(key)
        if value is not None and str(value) != "":
            return str(value).lower()
    return "unknown"


def plan_item_type_counts(items: Sequence[PlannedItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        item_type = item.fs42_type
        counts[item_type] = counts.get(item_type, 0) + 1
    return counts


def plan_item_type_summary(items: Sequence[PlannedItem]) -> dict[str, Any]:
    commercial_paths = [str(item.ffmpeg_input or item.resolved_path) for item in items if _is_commercial_type(item.fs42_type)]
    return {
        "plan_item_count": len(items),
        "plan_item_counts": plan_item_type_counts(items),
        "commercial_count": len(commercial_paths),
        "ad_count": len(commercial_paths),
        "commercial_paths": commercial_paths,
        "commercial_path_count": len(commercial_paths),
    }


def _is_commercial_type(item_type: str) -> bool:
    return item_type.lower() in {"commercial", "advert", "ad"}
