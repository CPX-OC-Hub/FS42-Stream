from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class BlockPlayoutDecision:
    source_block: Mapping[str, Any]
    render_plan: list[dict[str, Any]]
    render_state: "RenderableBlockState"
    snapshot: dict[str, Any]
    catch_up: dict[str, Any]


@dataclass(frozen=True)
class PlayoutProjection:
    current_block: dict[str, Any] | None
    next_block: dict[str, Any] | None
    current_item: dict[str, Any] | None
    next_item: dict[str, Any] | None
    snapshot: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.snapshot)
        payload["current_block"] = dict(self.current_block) if isinstance(self.current_block, Mapping) else None
        payload["next_block"] = dict(self.next_block) if isinstance(self.next_block, Mapping) else None
        payload["current_item"] = dict(self.current_item) if isinstance(self.current_item, Mapping) else None
        payload["next_item"] = dict(self.next_item) if isinstance(self.next_item, Mapping) else None
        return payload


@dataclass(frozen=True)
class RenderableBlockState:
    title: str
    start_time: str | None
    end_time: str | None
    selection: "BlockSelectionState"
    source: Mapping[str, Any]
    plan: list[dict[str, Any]]


@dataclass(frozen=True)
class BlockSelectionState:
    index: int | None
    reason: str | None
    block_title: str
    start_time: str | None
    end_time: str | None


def build_block_timeline(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    block_start = _parse_datetime(block.get("start_time"))
    raw_plan = block.get("plan")
    if block_start is None or not isinstance(raw_plan, list):
        return []

    cursor = block_start
    timeline: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_plan):
        if not isinstance(raw_item, Mapping):
            continue
        duration = _float_or_zero(raw_item.get("duration"))
        skip = _float_or_zero(raw_item.get("skip"))
        wallclock_start = cursor
        wallclock_end = wallclock_start + _seconds(duration)
        timeline.append(
            {
                "index": index,
                "content_type": raw_item.get("content_type") or raw_item.get("type"),
                "media_type": raw_item.get("media_type"),
                "path": raw_item.get("path") or raw_item.get("realpath"),
                "duration": duration,
                "skip": skip,
                "is_stream": raw_item.get("is_stream"),
                "wallclock_start": wallclock_start.isoformat(),
                "wallclock_end": wallclock_end.isoformat(),
                "media_seek_start": skip,
                "media_seek_end": skip + duration,
            }
        )
        cursor = wallclock_end
    return timeline


def playout_snapshot(block: Mapping[str, Any], *, now: datetime | None, schedule_timezone: str | None = None) -> dict[str, Any]:
    timeline = build_block_timeline(block)
    block_start = _parse_datetime(block.get("start_time"))
    block_end = _parse_datetime(block.get("end_time"))
    schedule_now = _schedule_now(now, schedule_timezone) if now is not None else None
    current_item = _current_timeline_item(timeline, schedule_now)
    next_item = _next_timeline_item(timeline, current_index=current_item.get("index") if current_item else None, now=schedule_now)
    plan_duration = sum(_float_or_zero(item.get("duration")) for item in timeline)
    return {
        "block_title": str(block.get("title") or "untitled"),
        "block_start": block_start.isoformat() if block_start is not None else None,
        "block_end": block_end.isoformat() if block_end is not None else None,
        "schedule_now": schedule_now.isoformat() if schedule_now is not None else None,
        "block_elapsed": max(0.0, (schedule_now - block_start).total_seconds()) if schedule_now is not None and block_start is not None else None,
        "plan_duration": plan_duration,
        "timeline": timeline,
        "current_item": current_item,
        "next_item": next_item,
    }


def trim_block_to_wallclock(block: Mapping[str, Any], *, now: datetime | None, schedule_timezone: str | None) -> tuple[Mapping[str, Any], dict[str, Any]]:
    if now is None:
        return block, {"applied": False, "reason": "no_now"}
    block_start = _parse_datetime(block.get("start_time"))
    if block_start is None:
        return block, {"applied": False, "reason": "missing_block_start"}
    schedule_now = _schedule_now(now, schedule_timezone)
    elapsed = (schedule_now - block_start).total_seconds()
    if elapsed <= 0:
        return block, {"applied": False, "reason": "before_or_at_block_start", "block_elapsed": max(0.0, elapsed)}
    raw_plan = block.get("plan")
    if not isinstance(raw_plan, list):
        return block, {"applied": False, "reason": "missing_plan", "block_elapsed": elapsed}

    cursor = 0.0
    for index, item in enumerate(raw_plan):
        if not isinstance(item, Mapping):
            continue
        duration = _float_or_zero(item.get("duration"))
        item_end = cursor + duration
        if cursor <= elapsed < item_end:
            offset_in_item = elapsed - cursor
            adjusted_first = dict(item)
            original_skip = _float_or_zero(adjusted_first.get("skip"))
            adjusted_first["skip"] = original_skip + offset_in_item
            adjusted_first["duration"] = max(0.0, duration - offset_in_item)
            adjusted_first["catch_up_offset"] = offset_in_item
            trimmed_plan = [adjusted_first]
            trimmed_plan.extend(dict(next_item) for next_item in raw_plan[index + 1 :] if isinstance(next_item, Mapping))
            caught_block = dict(block)
            caught_block["plan"] = trimmed_plan
            return caught_block, {
                "applied": True,
                "schedule_now": schedule_now.isoformat(),
                "block_elapsed": elapsed,
                "start_plan_index": index,
                "offset_in_item": offset_in_item,
                "original_skip": original_skip,
                "media_seek": original_skip + offset_in_item,
                "remaining_item_duration": max(0.0, duration - offset_in_item),
                "dropped_plan_items": index,
                "current_content_type": item.get("content_type") or item.get("type") or item.get("media_type"),
                "current_path": item.get("path") or item.get("realpath"),
            }
        cursor = item_end
    return block, {"applied": False, "reason": "after_plan_end", "block_elapsed": elapsed, "plan_duration": cursor}


def resolve_block_playout(
    block: Mapping[str, Any],
    *,
    now: datetime | None,
    schedule_timezone: str | None,
    selection_index: int | None = None,
    selection_reason: str | None = None,
) -> BlockPlayoutDecision:
    snapshot = playout_snapshot(block, now=now, schedule_timezone=schedule_timezone)
    render_block, catch_up = trim_block_to_wallclock(block, now=now, schedule_timezone=schedule_timezone)
    raw_render_plan = render_block.get("plan")
    render_plan = [dict(item) for item in raw_render_plan if isinstance(item, Mapping)] if isinstance(raw_render_plan, list) else []
    title = str(render_block.get("title") or block.get("title") or "untitled")
    start_time = render_block.get("start_time") or block.get("start_time")
    end_time = render_block.get("end_time") or block.get("end_time")
    render_state = RenderableBlockState(
        title=title,
        start_time=start_time,
        end_time=end_time,
        selection=BlockSelectionState(
            index=selection_index,
            reason=selection_reason,
            block_title=title,
            start_time=start_time,
            end_time=end_time,
        ),
        source=block,
        plan=render_plan,
    )
    return BlockPlayoutDecision(
        source_block=block,
        render_plan=render_plan,
        render_state=render_state,
        snapshot=snapshot,
        catch_up=dict(catch_up),
    )


def build_playout_projection(
    decision: BlockPlayoutDecision,
    *,
    next_block: Mapping[str, Any] | None = None,
    schedule_now: datetime | None = None,
) -> PlayoutProjection:
    snapshot = dict(decision.snapshot)
    if schedule_now is not None:
        snapshot["schedule_now"] = schedule_now.isoformat()
    selection = decision.render_state.selection
    current_block = {
        "index": selection.index,
        "selection_reason": selection.reason,
        "title": selection.block_title,
        "start_time": selection.start_time,
        "end_time": selection.end_time,
        "plan": [dict(item) for item in decision.render_state.plan],
    }
    current_item = dict(snapshot.get("current_item")) if isinstance(snapshot.get("current_item"), Mapping) else None
    next_item = dict(snapshot.get("next_item")) if isinstance(snapshot.get("next_item"), Mapping) else None
    return PlayoutProjection(
        current_block=current_block,
        next_block=dict(next_block) if isinstance(next_block, Mapping) else None,
        current_item=current_item,
        next_item=next_item,
        snapshot=snapshot,
    )


def project_playout_state(
    decision: BlockPlayoutDecision,
    *,
    next_block: Mapping[str, Any] | None = None,
    schedule_now: datetime | None = None,
) -> dict[str, Any]:
    return build_playout_projection(decision, next_block=next_block, schedule_now=schedule_now).as_dict()


def _current_timeline_item(timeline: Sequence[Mapping[str, Any]], now: datetime | None) -> dict[str, Any] | None:
    if now is None:
        return None
    for item in timeline:
        start = _parse_datetime(item.get("wallclock_start"))
        end = _parse_datetime(item.get("wallclock_end"))
        if start is None or end is None:
            continue
        if start <= now < end:
            current = dict(item)
            offset = (now - start).total_seconds()
            current["current_offset_in_item"] = offset
            current["media_seek"] = _float_or_zero(item.get("media_seek_start")) + offset
            return current
    return None


def _next_timeline_item(timeline: Sequence[Mapping[str, Any]], *, current_index: Any, now: datetime | None) -> dict[str, Any] | None:
    if current_index is not None:
        for item in timeline:
            if item.get("index") == current_index + 1:
                return dict(item)
        return None
    if now is None:
        return dict(timeline[0]) if timeline else None
    for item in timeline:
        start = _parse_datetime(item.get("wallclock_start"))
        if start is not None and start > now:
            return dict(item)
    return None


def _seconds(value: float):
    from datetime import timedelta

    return timedelta(seconds=value)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _schedule_now(now: datetime | None, schedule_timezone: str | None) -> datetime:
    if schedule_timezone:
        zone = ZoneInfo(schedule_timezone)
        source = now or datetime.now(timezone.utc)
        if source.tzinfo is None:
            source = source.replace(tzinfo=zone)
        return source.astimezone(zone).replace(tzinfo=None)
    return now or datetime.now()


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
