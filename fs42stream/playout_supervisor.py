from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .playout_engine import BlockPlayoutDecision, PlayoutProjection, build_playout_projection, resolve_block_playout
from .run_block import DEFAULT_SCHEDULE_TIMEZONE, SelectedBlock, _schedule_now, select_current_or_next_block


@dataclass(frozen=True)
class SupervisedPlayout:
    selected: SelectedBlock
    decision: BlockPlayoutDecision
    projection: PlayoutProjection
    upcoming_blocks: list[dict[str, Any]]

    @property
    def active_block(self) -> dict[str, Any] | None:
        return dict(self.projection.current_block) if self.projection.current_block is not None else None

    @property
    def next_block(self) -> dict[str, Any] | None:
        return dict(self.projection.next_block) if self.projection.next_block is not None else None

    @property
    def current_item(self) -> dict[str, Any] | None:
        return dict(self.projection.current_item) if self.projection.current_item is not None else None

    @property
    def next_item(self) -> dict[str, Any] | None:
        return dict(self.projection.next_item) if self.projection.next_item is not None else None

    def projection_dict(self) -> dict[str, Any]:
        return self.projection.as_dict()

    def contract_dict(self, *, schedule_now: datetime | None = None) -> dict[str, Any]:
        projection = self.projection_dict()
        if schedule_now is not None:
            projection["schedule_now"] = schedule_now.isoformat()
        source_plan = self.decision.source_block.get("plan")
        source_plan_item_count = len(source_plan) if isinstance(source_plan, list) else 0
        return {
            "selection": {"index": self.selected.index, "reason": self.selected.reason},
            "schedule_now": projection.get("schedule_now"),
            "current_block": self.active_block,
            "next_block": self.next_block,
            "upcoming_blocks": [dict(block) for block in self.upcoming_blocks],
            "timeline": [dict(item) for item in projection.get("timeline", []) if isinstance(item, Mapping)],
            "catch_up": dict(self.decision.catch_up),
            "render_plan_item_count": len(self.decision.render_state.plan),
            "source_plan_item_count": source_plan_item_count,
            "current_item": self.current_item,
            "next_item": self.next_item,
            "playout": projection,
        }

    def status_fields(self, *, schedule_now: datetime | None = None) -> dict[str, Any]:
        contract = self.contract_dict(schedule_now=schedule_now)
        return {
            "supervisor": contract,
            "active_block": contract["current_block"],
            "upcoming_blocks": contract["upcoming_blocks"],
            "catch_up": dict(contract["catch_up"]),
            "current_plan_item": contract["current_item"],
            "next_plan_item": contract["next_item"],
            "playout": dict(contract["playout"]),
        }


def supervise_schedule_playout(
    schedule: Mapping[str, Any],
    *,
    now: datetime | None,
    schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE,
    minimum_index: int = 0,
    upcoming_limit: int = 5,
) -> SupervisedPlayout:
    selected = _select_not_before(
        schedule,
        now=now,
        minimum_index=minimum_index,
        schedule_timezone=schedule_timezone,
    )
    decision = resolve_block_playout(
        selected.block,
        now=now,
        schedule_timezone=schedule_timezone,
        selection_index=selected.index,
        selection_reason=selected.reason,
    )
    upcoming_blocks = _upcoming_blocks(schedule, after_index=selected.index, limit=upcoming_limit)
    projection = build_playout_projection(
        decision,
        next_block=upcoming_blocks[0] if upcoming_blocks else None,
        schedule_now=_schedule_now(now, schedule_timezone) if now is not None else None,
    )
    return SupervisedPlayout(
        selected=selected,
        decision=decision,
        projection=projection,
        upcoming_blocks=upcoming_blocks,
    )



def _select_not_before(
    schedule: Mapping[str, Any],
    *,
    now: datetime | None,
    minimum_index: int,
    schedule_timezone: str | None,
) -> SelectedBlock:
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
