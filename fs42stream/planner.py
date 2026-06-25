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


@dataclass(frozen=True)
class PlannedBlock:
    title: str
    start_time: str | None
    end_time: str | None
    source: Mapping[str, Any]
    items: Sequence[PlannedItem]


class BlockPlanner:
    """Build block-level plans without flattening or rewriting FS42's own plan entries."""

    def __init__(self, resolver: PathResolver | None = None, probe: FFProbe | None = None) -> None:
        self.resolver = resolver or PathResolver()
        self.probe = probe or FFProbe()

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
        return PlannedItem(
            source=item,
            resolved_path=resolved,
            skip=float(item.get("skip") or 0.0),
            duration=float(item.get("duration") or 0.0),
            probe=self.probe.validate_video(resolved),
        )
