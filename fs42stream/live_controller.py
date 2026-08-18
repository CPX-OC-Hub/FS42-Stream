from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .client import DEFAULT_SCHEDULE_BASE_PATH, DEFAULT_SCHEDULE_HOST, DEFAULT_SCHEDULE_PORT, DEFAULT_SCHEDULE_SCHEME, FS42ScheduleClient, build_schedule_api_base_url
from .ffmpeg import FFMpegHLSCommandBuilder, PlayoutMode, StreamProfile, hls_list_size_for_profile
from .jellyfin_preroll import GatedJellyfinPreRoll, staged_run_timing
from .ffprobe import FFProbe
from .paths import PathResolver
from .planner import DEFAULT_BRB_IMAGE_PATH, BlockPlanner
from .playout_supervisor import SupervisedPlayout, supervise_schedule_playout
from .run_block import (
    _build_stale_placeholder_schedule,
    _stale_schedule_state,
    DEFAULT_API_BASE_URL,
    DEFAULT_CHANNEL,
    DEFAULT_FFMPEG,
    DEFAULT_FFPROBE,
    DEFAULT_FS42_ROOT,
    DEFAULT_SCHEDULE_TIMEZONE,
    DEFAULT_SDTV_ROOT,
    HLS_TARGET_SEGMENT_DURATION,
    BlockRunConfig,
    BlockRunner,
    _hls_segment_duration_since,
    _next_hls_start_number,
    _normalize_jellyfin_live_playlist,
    _parse_datetime,
    _rewrite_live_playlist_boundaries,
    _schedule_now,
)


BOUNDARY_FILLER_STARTUP_GUARD_SECONDS = 20.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _transition_safe_filler_duration(duration: float) -> float:
    """Round filler up to complete HLS segment cadence to avoid tiny tail segments."""

    if duration <= 0:
        raise ValueError("duration must be positive")
    return math.ceil(duration / HLS_TARGET_SEGMENT_DURATION) * HLS_TARGET_SEGMENT_DURATION


@dataclass(frozen=True)
class LiveControllerConfig:
    channel: str = DEFAULT_CHANNEL
    output_root: Path = Path("/tmp/fs42stream-live")
    max_blocks: int = 2
    duration_limit: float = 10.0
    now: datetime | None = None
    dry_run: bool = False
    status_callback: Callable[[Mapping[str, Any]], None] | None = None
    schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE
    max_recovery_attempts_per_block: int = 50
    clock: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], None] = time.sleep
    stream_profile: StreamProfile = "direct"
    playout_mode: PlayoutMode = "ts-primary"
    shared_schedule_clock: Any | None = None
    hls_retention_max_age_seconds: float = 6 * 60 * 60
    hls_retention_max_segments_per_dir: int = 7200
    brb_image_path: str | Path = DEFAULT_BRB_IMAGE_PATH
    jellyfin_pre_roll_lead_seconds: float = 0.0
    jellyfin_pre_roll_min_buffer_seconds: float = 30.0
    jellyfin_pre_roll_max_publish_delay_seconds: float = 60.0


class ScheduleClient(Protocol):
    def fetch_schedule(self, channel: str, *, expected_blocks: int | None = None) -> Mapping[str, Any]: ...


class SingleBlockRunner(Protocol):
    def run(self, config: BlockRunConfig) -> Mapping[str, Any]: ...


class BoundaryFillerRunner(Protocol):
    def run(self, *, output_dir: Path, output_name: str, duration: float, hls_start_number: int, hls_start_time_offset: float | None = None, hls_append: bool = True, stream_profile: StreamProfile = "direct") -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class SharedScheduleSelection:
    schedule: Mapping[str, Any]
    selection_now: datetime
    playout_state: SupervisedPlayout
    stale_schedule: dict[str, Any] | None


class SharedScheduleBlockClock:
    """Barrier-backed schedule/block selector shared by integrated profiles."""

    def __init__(self, *, profile_count: int, timeout: float = 90.0) -> None:
        if profile_count <= 0:
            raise ValueError("profile_count must be positive")
        self.profile_count = profile_count
        self.timeout = timeout
        self._condition = threading.Condition()
        self._states: dict[int, dict[str, Any]] = {}

    def select(
        self,
        *,
        ordinal: int,
        profile: StreamProfile,
        config: LiveControllerConfig,
        schedule_client: ScheduleClient,
        cursor: datetime | None,
        simulated_cursor: bool,
        minimum_index: int,
    ) -> SharedScheduleSelection:
        with self._condition:
            state = self._states.setdefault(ordinal, {"arrivals": set(), "released": 0, "selection": None})
            arrivals = state["arrivals"]
            if not isinstance(arrivals, set):
                raise RuntimeError("shared schedule clock state is corrupt")
            arrivals.add(profile)
            if state.get("selection") is None:
                state["selection"] = _select_shared_schedule_block(
                    config=config,
                    schedule_client=schedule_client,
                    cursor=cursor,
                    simulated_cursor=simulated_cursor,
                    minimum_index=minimum_index,
                )
            self._condition.notify_all()
            if not self._condition.wait_for(lambda: len(arrivals) >= self.profile_count, timeout=self.timeout):
                missing = self.profile_count - len(arrivals)
                raise RuntimeError(f"shared schedule clock timed out at block {ordinal + 1}; {missing} profile(s) did not reach the boundary")
            selection = state.get("selection")
            if not isinstance(selection, SharedScheduleSelection):
                raise RuntimeError("shared schedule clock did not produce a selection")
            state["released"] = int(state.get("released") or 0) + 1
            if state["released"] >= self.profile_count:
                self._states.pop(ordinal - 1, None)
            return selection


def _select_shared_schedule_block(
    *,
    config: LiveControllerConfig,
    schedule_client: ScheduleClient,
    cursor: datetime | None,
    simulated_cursor: bool,
    minimum_index: int,
) -> SharedScheduleSelection:
    schedule = schedule_client.fetch_schedule(config.channel, expected_blocks=None)
    selection_now = cursor if simulated_cursor and cursor is not None else config.clock()
    summary = None
    if hasattr(schedule_client, "fetch_schedule_summary"):
        try:
            summary = schedule_client.fetch_schedule_summary(config.channel)  # type: ignore[attr-defined]
        except Exception:
            summary = None
    stale_schedule = _stale_schedule_state(summary, channel=config.channel, now=selection_now, schedule_timezone=config.schedule_timezone)
    if stale_schedule and stale_schedule.get("active"):
        schedule = _build_stale_placeholder_schedule(config.channel, now=selection_now, duration_limit=config.duration_limit, schedule_timezone=config.schedule_timezone, brb_image_path=config.brb_image_path)
        stale_schedule["brb_image_path"] = str(config.brb_image_path)
    playout_state = supervise_schedule_playout(
        schedule,
        now=selection_now,
        schedule_timezone=config.schedule_timezone,
        minimum_index=minimum_index,
    )
    return SharedScheduleSelection(
        schedule=schedule,
        selection_now=selection_now,
        playout_state=playout_state,
        stale_schedule=dict(stale_schedule) if isinstance(stale_schedule, Mapping) else None,
    )


class HLSBlackSlateFillerRunner:
    def __init__(self, ffmpeg: str = DEFAULT_FFMPEG) -> None:
        self.ffmpeg = ffmpeg

    def run(self, *, output_dir: Path, output_name: str, duration: float, hls_start_number: int, hls_start_time_offset: float | None = None, hls_append: bool = True, stream_profile: StreamProfile = "direct") -> Mapping[str, Any]:
        if duration <= 0:
            raise ValueError("duration must be positive")
        if hls_start_time_offset is not None and hls_start_time_offset < 0:
            raise ValueError("hls_start_time_offset must be non-negative")
        output_dir.mkdir(parents=True, exist_ok=True)
        playlist = output_dir / f"{output_name}.m3u8"
        segment_pattern = output_dir / f"{output_name}_%05d.ts"
        safe_duration = _transition_safe_filler_duration(duration)
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-y",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x480:r=25",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            _num(safe_duration),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-g",
            "50",
            "-keyint_min",
            "50",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            str(hls_list_size_for_profile(stream_profile)),
        ]
        if stream_profile != "jellyfin" or not hls_append:
            command.extend([
                "-start_number",
                str(hls_start_number),
            ])
        if hls_start_time_offset is not None:
            command.extend(["-output_ts_offset", _num(hls_start_time_offset)])
        if hls_append:
            command.extend(["-hls_flags", "omit_endlist+append_list"])
        else:
            command.extend(["-hls_flags", "omit_endlist"])
        command.extend(["-hls_segment_filename", str(segment_pattern), str(playlist)])
        completed = subprocess.run(command, check=False, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if stream_profile == "jellyfin" and playlist.exists():
            _normalize_jellyfin_live_playlist(playlist)
        hls_next_start_number = _next_hls_start_number(playlist, fallback=hls_start_number)
        observed_segment_duration = _hls_segment_duration_since(playlist, start_number=hls_start_number)
        segment_duration = min(observed_segment_duration, safe_duration)
        return {
            "status": "ok" if completed.returncode == 0 else "ffmpeg-error",
            "playlist": str(playlist),
            "command": command,
            "hls_start_number": hls_start_number,
            "hls_start_time_offset": hls_start_time_offset,
            "requested_duration": duration,
            "duration": safe_duration,
            "hls_next_start_number": hls_next_start_number,
            "hls_segment_duration": segment_duration,
            "hls_next_start_time_offset": (hls_start_time_offset + segment_duration) if hls_start_time_offset is not None else None,
            "hls": {"playlist": str(playlist), "segment_count": max(0, hls_next_start_number - hls_start_number)},
            "ffmpeg": {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr},
        }


class LiveController:
    """Bounded live block lifecycle controller for stable per-channel HLS output."""

    def __init__(self, *, schedule_client: ScheduleClient | None = None, block_runner: SingleBlockRunner | None = None, filler_runner: BoundaryFillerRunner | None = None) -> None:
        self.schedule_client = schedule_client or FS42ScheduleClient(DEFAULT_API_BASE_URL)
        self.block_runner = block_runner or BlockRunner()
        self.filler_runner = filler_runner or HLSBlackSlateFillerRunner()

    def run(self, config: LiveControllerConfig) -> dict[str, Any]:
        if config.max_blocks <= 0:
            raise ValueError("max_blocks must be positive")
        if config.duration_limit <= 0:
            raise ValueError("duration_limit must be positive")
        if config.jellyfin_pre_roll_lead_seconds < 0:
            raise ValueError("jellyfin_pre_roll_lead_seconds must be non-negative")
        if config.jellyfin_pre_roll_min_buffer_seconds < 0:
            raise ValueError("jellyfin_pre_roll_min_buffer_seconds must be non-negative")
        if config.jellyfin_pre_roll_max_publish_delay_seconds < 0:
            raise ValueError("jellyfin_pre_roll_max_publish_delay_seconds must be non-negative")
        if config.stream_profile not in {"direct", "jellyfin"}:
            raise ValueError("stream_profile must be 'direct' or 'jellyfin'")
        if config.playout_mode not in {"hls-primary", "ts-primary"}:
            raise ValueError("playout_mode must be 'hls-primary' or 'ts-primary'")

        channel_output_dir = config.output_root / FFMpegHLSCommandBuilder._slug(config.channel)
        if config.stream_profile == "jellyfin":
            channel_output_dir = channel_output_dir / "jellyfin"
        channel_output_dir.mkdir(parents=True, exist_ok=True)

        events: list[dict[str, Any]] = []
        cursor = config.now
        simulated_cursor = config.now is not None
        last_index = -1
        last_playout_state: SupervisedPlayout | None = None
        last_schedule_now: datetime | None = None
        last_stale_schedule: dict[str, Any] | None = None
        last_ffmpeg_status: dict[str, Any] | None = None
        previous_block_info: dict[str, Any] | None = None
        active_pre_roll: dict[str, Any] | None = None
        public_resume: dict[str, Any] | None = None
        last_pre_roll_status: dict[str, Any] | None = None

        hls_start_number = 0
        hls_start_time_offset = 0.0
        hls_boundary_starts: list[int] = []
        for ordinal in range(config.max_blocks):
            minimum_index = last_index + 1 if simulated_cursor else 0
            if config.shared_schedule_clock is not None:
                shared_selection = config.shared_schedule_clock.select(
                    ordinal=ordinal,
                    profile=config.stream_profile,
                    config=config,
                    schedule_client=self.schedule_client,
                    cursor=cursor,
                    simulated_cursor=simulated_cursor,
                    minimum_index=minimum_index,
                )
                schedule = shared_selection.schedule
                selection_now = shared_selection.selection_now
                playout_state = shared_selection.playout_state
                stale_schedule = shared_selection.stale_schedule
            else:
                shared_selection = _select_shared_schedule_block(
                    config=config,
                    schedule_client=self.schedule_client,
                    cursor=cursor,
                    simulated_cursor=simulated_cursor,
                    minimum_index=minimum_index,
                )
                schedule = shared_selection.schedule
                selection_now = shared_selection.selection_now
                playout_state = shared_selection.playout_state
                stale_schedule = shared_selection.stale_schedule
            selected = playout_state.selected
            playout = playout_state.decision
            last_playout_state = playout_state
            last_schedule_now = selection_now
            last_stale_schedule = dict(stale_schedule) if isinstance(stale_schedule, Mapping) else None
            block_info = dict(playout_state.active_block or {})
            cleanup = clean_hls_outputs(channel_output_dir) if ordinal == 0 else []
            events.append(
                {
                    "event": "block_start",
                    "block_number": ordinal + 1,
                    "block": block_info,
                    "current_item": playout_state.current_item,
                    "next_item": playout_state.next_item,
                    "playout": playout_state.projection_dict(),
                    "channel_output_dir": str(channel_output_dir),
                    "cleaned_stale_outputs": [str(path) for path in cleanup],
                }
            )
            _emit_live_status(
                config,
                status="running",
                channel_output_dir=channel_output_dir,
                events=events,
                playout_state=playout_state,
                schedule_now=selection_now,
                stale_schedule=last_stale_schedule,
            )
            block_start = _parse_datetime(playout.render_state.start_time)
            block_end = _parse_datetime(playout.render_state.end_time)
            run_now = selection_now if not simulated_cursor else (block_start or cursor)
            if public_resume is not None and public_resume.get("selected_index") == selected.index:
                run_now = public_resume["run_now"]
                hls_start_number = int(public_resume["hls_start_number"])
                hls_start_time_offset = float(public_resume["hls_start_time_offset"])
                public_resume = None
            if (
                config.stream_profile == "jellyfin"
                and config.jellyfin_pre_roll_lead_seconds > 0
                and ordinal < config.max_blocks - 1
                and block_end is not None
            ):
                raw_blocks = schedule.get("schedule_blocks")
                next_index = selected.index + 1
                if isinstance(raw_blocks, list) and next_index < len(raw_blocks) and isinstance(raw_blocks[next_index], Mapping):
                    next_block = raw_blocks[next_index]
                    next_start = _parse_datetime(next_block.get("start_time"))
                    next_end = _parse_datetime(next_block.get("end_time"))
                    if next_start is not None:
                        stage_dir = config.output_root / ".jellyfin-staging" / FFMpegHLSCommandBuilder._slug(config.channel) / f"{ordinal + 1:04d}-{next_start.strftime('%Y%m%dT%H%M%S')}"
                        stage_dir.mkdir(parents=True, exist_ok=True)
                        for stale_path in stage_dir.glob("*"):
                            if stale_path.is_file():
                                stale_path.unlink()
                        stage_playlist = stage_dir / f"{FFMpegHLSCommandBuilder._slug(config.channel)}.m3u8"
                        stage_duration = min(
                            max(config.jellyfin_pre_roll_lead_seconds, config.jellyfin_pre_roll_min_buffer_seconds),
                            max(0.0, (next_end - next_start).total_seconds()) if next_end is not None else max(config.jellyfin_pre_roll_lead_seconds, config.jellyfin_pre_roll_min_buffer_seconds),
                        )
                        stage_result: dict[str, Any] = {}

                        def run_stage(stage_started_at: datetime) -> None:
                            timing = staged_run_timing(block_start=next_start, stage_started_at=stage_started_at)
                            stage_result.update(
                                self.block_runner.run(
                                    BlockRunConfig(
                                        channel=config.channel,
                                        duration_limit=stage_duration,
                                        output_dir=stage_dir,
                                        now=timing.run_now,
                                        dry_run=config.dry_run,
                                        output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                                        hls_start_number=0,
                                        hls_start_time_offset=0.0,
                                        hls_append=False,
                                        hls_boundary_starts=(),
                                        schedule_timezone=config.schedule_timezone,
                                        stream_profile="jellyfin",
                                        playout_mode=config.playout_mode,
                                        schedule=schedule,
                                        selected_block_index=next_index,
                                        selected_block_reason="pre-roll",
                                    )
                                )
                            )

                        if simulated_cursor:
                            run_stage(next_start - timedelta(seconds=config.jellyfin_pre_roll_lead_seconds))
                            stage_thread = None
                        else:
                            def delayed_stage() -> None:
                                start_at = next_start - timedelta(seconds=config.jellyfin_pre_roll_lead_seconds)
                                delay = max(0.0, (start_at - _schedule_now(config.clock(), config.schedule_timezone)).total_seconds())
                                if delay:
                                    config.sleep(delay)
                                run_stage(_schedule_now(config.clock(), config.schedule_timezone))

                            stage_thread = threading.Thread(target=delayed_stage, name=f"jellyfin-preroll-{ordinal + 1}", daemon=True)
                            stage_thread.start()
                        active_pre_roll = {
                            "next_index": next_index,
                            "next_start": next_start,
                            "next_block": dict(next_block),
                            "stage_playlist": stage_playlist,
                            "stage_result": stage_result,
                            "stage_thread": stage_thread,
                        }
                        last_pre_roll_status = {
                            "pre_roll_state": "staged-private",
                            "staged_block": str(next_block.get("title") or FFMpegHLSCommandBuilder._slug(config.channel)),
                            "staged_segments_ready": 0,
                            "staged_duration_ready_seconds": 0.0,
                            "minimum_ready_duration_seconds": config.jellyfin_pre_roll_min_buffer_seconds,
                            "minimum_ready_segments": 0,
                            "publish_held_for_buffer": False,
                            "publish_delay_seconds": 0.0,
                            "publish_at": next_start.isoformat(),
                        }
                        _emit_live_status(config, status="running", channel_output_dir=channel_output_dir, events=events, playout_state=playout_state, schedule_now=selection_now, stale_schedule=last_stale_schedule, pre_roll=last_pre_roll_status)
            effective_duration_limit = config.duration_limit
            if not simulated_cursor and block_end is not None:
                schedule_now = _schedule_now(selection_now, config.schedule_timezone)
                remaining_seconds = max(0.0, (block_end - schedule_now).total_seconds())
                if remaining_seconds > 0:
                    effective_duration_limit = min(config.duration_limit, remaining_seconds)

            block_hls_append = ordinal > 0
            block_hls_start_number = hls_start_number
            def emit_ffmpeg_status(raw_status: Mapping[str, Any]) -> None:
                nonlocal last_ffmpeg_status
                ffmpeg_status = dict(raw_status)
                ffmpeg_status.setdefault("profile", config.stream_profile)
                last_ffmpeg_status = ffmpeg_status
                _emit_live_status(
                    config,
                    status="running",
                    channel_output_dir=channel_output_dir,
                    events=events,
                    playout_state=playout_state,
                    schedule_now=selection_now,
                    stale_schedule=last_stale_schedule,
                    ffmpeg=ffmpeg_status,
                )

            diagnostics = self.block_runner.run(
                BlockRunConfig(
                    channel=config.channel,
                    duration_limit=effective_duration_limit,
                    output_dir=channel_output_dir,
                    now=run_now,
                    dry_run=config.dry_run,
                    output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                    hls_start_number=block_hls_start_number,
                    hls_start_time_offset=hls_start_time_offset,
                    hls_append=block_hls_append,
                    hls_boundary_starts=tuple(hls_boundary_starts),
                    schedule_timezone=config.schedule_timezone,
                    stream_profile=config.stream_profile,
                    playout_mode=config.playout_mode,
                    schedule=schedule,
                    selected_block_index=selected.index,
                    selected_block_reason=selected.reason,
                    ffmpeg_status_callback=emit_ffmpeg_status,
                )
            )
            diagnostics_dict = _finalize_block_run_diagnostics(
                diagnostics,
                simulated_cursor=simulated_cursor,
                run_started_at=selection_now,
                clock=config.clock,
                duration_limit=effective_duration_limit,
                schedule_timezone=config.schedule_timezone,
            )
            if isinstance(diagnostics_dict.get("hls_boundary_starts"), list):
                hls_boundary_starts = [int(number) for number in diagnostics_dict["hls_boundary_starts"]]
            hls_start_number = int(diagnostics_dict.get("hls_next_start_number") or hls_start_number)
            hls_start_time_offset = _next_hls_start_time_offset(diagnostics_dict, fallback=hls_start_time_offset)
            events.append(_complete_event(ordinal=ordinal, block=block_info, diagnostics=diagnostics_dict))
            _emit_live_status(
                config,
                status="running",
                channel_output_dir=channel_output_dir,
                events=events,
                playout_state=playout_state,
                schedule_now=selection_now,
                stale_schedule=last_stale_schedule,
                ffmpeg=last_ffmpeg_status,
            )

            if not simulated_cursor and _block_run_failed(diagnostics_dict):
                recovery_attempt = 0
                while recovery_attempt < config.max_recovery_attempts_per_block and block_end is not None:
                    recovery_attempt += 1
                    recovery_now = config.clock()
                    schedule_now = _schedule_now(recovery_now, config.schedule_timezone)
                    if schedule_now >= block_end:
                        break
                    recovery_playout_state = supervise_schedule_playout(
                        schedule,
                        now=recovery_now,
                        schedule_timezone=config.schedule_timezone,
                        minimum_index=selected.index,
                    )
                    recovery_selected = recovery_playout_state.selected
                    if recovery_selected.index != selected.index:
                        break
                    recovery_playout = recovery_playout_state.decision
                    recovery_block_info = dict(recovery_playout_state.active_block or {})
                    def emit_recovery_ffmpeg_status(raw_status: Mapping[str, Any]) -> None:
                        nonlocal last_ffmpeg_status
                        ffmpeg_status = dict(raw_status)
                        ffmpeg_status.setdefault("profile", config.stream_profile)
                        last_ffmpeg_status = ffmpeg_status
                        _emit_live_status(
                            config,
                            status="recovering",
                            channel_output_dir=channel_output_dir,
                            events=events,
                            playout_state=recovery_playout_state,
                            schedule_now=recovery_now,
                            stale_schedule=last_stale_schedule,
                            ffmpeg=ffmpeg_status,
                        )

                    events.append(
                        {
                            "event": "block_recovery",
                            "block_number": ordinal + 1,
                            "attempt": recovery_attempt,
                            "reason": str(diagnostics_dict.get("status") or "ffmpeg-error"),
                            "block": recovery_block_info,
                            "current_item": recovery_playout_state.current_item,
                            "next_item": recovery_playout_state.next_item,
                            "playout": recovery_playout_state.projection_dict(),
                            "recover_at": schedule_now.isoformat(),
                            "previous_status": diagnostics_dict.get("status"),
                            "previous_ffmpeg_returncode": _ffmpeg_returncode(diagnostics_dict),
                        }
                    )
                    _emit_live_status(
                        config,
                        status="recovering",
                        channel_output_dir=channel_output_dir,
                        events=events,
                        playout_state=recovery_playout_state,
                        schedule_now=recovery_now,
                        stale_schedule=last_stale_schedule,
                        ffmpeg=last_ffmpeg_status,
                    )
                    recovery_duration_limit = min(config.duration_limit, max(0.0, (block_end - schedule_now).total_seconds()))
                    diagnostics_dict = _finalize_block_run_diagnostics(
                        self.block_runner.run(
                            BlockRunConfig(
                                channel=config.channel,
                                duration_limit=recovery_duration_limit,
                                output_dir=channel_output_dir,
                                now=recovery_now,
                                dry_run=config.dry_run,
                                output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                                hls_start_number=hls_start_number,
                                hls_start_time_offset=hls_start_time_offset,
                                hls_append=True,
                                hls_boundary_starts=tuple(hls_boundary_starts),
                                schedule_timezone=config.schedule_timezone,
                                stream_profile=config.stream_profile,
                                playout_mode=config.playout_mode,
                                schedule=schedule,
                                selected_block_index=recovery_selected.index,
                                selected_block_reason=recovery_selected.reason,
                                ffmpeg_status_callback=emit_recovery_ffmpeg_status,
                            )
                        ),
                        simulated_cursor=simulated_cursor,
                        run_started_at=recovery_now,
                        clock=config.clock,
                        duration_limit=recovery_duration_limit,
                        schedule_timezone=config.schedule_timezone,
                    )
                    if isinstance(diagnostics_dict.get("hls_boundary_starts"), list):
                        hls_boundary_starts = [int(number) for number in diagnostics_dict["hls_boundary_starts"]]
                    hls_start_number = int(diagnostics_dict.get("hls_next_start_number") or hls_start_number)
                    hls_start_time_offset = _next_hls_start_time_offset(diagnostics_dict, fallback=hls_start_time_offset)
                    events.append(_complete_event(ordinal=ordinal, block=recovery_block_info, diagnostics=diagnostics_dict))
                    _emit_live_status(
                        config,
                        status="running",
                        channel_output_dir=channel_output_dir,
                        events=events,
                        playout_state=recovery_playout_state,
                        schedule_now=recovery_now,
                        stale_schedule=last_stale_schedule,
                        ffmpeg=last_ffmpeg_status,
                    )
                    if not _block_run_failed(diagnostics_dict):
                        selected = recovery_selected
                        playout = recovery_playout
                        block_info = recovery_block_info
                        playout_state = recovery_playout_state
                        last_playout_state = recovery_playout_state
                        last_schedule_now = recovery_now
                        break

            last_index = selected.index
            if (
                active_pre_roll is not None
                and active_pre_roll.get("next_index") == selected.index + 1
                and block_end is not None
                and (simulated_cursor or _schedule_now(config.clock(), config.schedule_timezone) >= block_end)
            ):
                gate = GatedJellyfinPreRoll(
                    public_playlist=channel_output_dir / f"{FFMpegHLSCommandBuilder._slug(config.channel)}.m3u8",
                    staged_playlist=active_pre_roll["stage_playlist"],
                    publish_at=active_pre_roll["next_start"],
                    public_next_segment_number=hls_start_number,
                    staged_block=str(active_pre_roll["next_block"].get("title") or "scheduled-block"),
                    minimum_ready_duration_seconds=config.jellyfin_pre_roll_min_buffer_seconds,
                )
                last_pre_roll_status = gate.publish_if_due(block_end)
                events.append({"event": "public_boundary_switch" if last_pre_roll_status.get("pre_roll_state") == "published" else "pre_roll_fallback", **last_pre_roll_status})
                if last_pre_roll_status.get("pre_roll_state") == "published":
                    staged_duration = _playlist_duration(active_pre_roll["stage_playlist"])
                    public_resume = {
                        "selected_index": active_pre_roll["next_index"],
                        "run_now": active_pre_roll["next_start"] + timedelta(seconds=staged_duration),
                        "hls_start_number": gate.public_next_segment_number,
                        "hls_start_time_offset": hls_start_time_offset + staged_duration,
                    }
                active_pre_roll = None if last_pre_roll_status.get("pre_roll_state") != "held-for-buffer" else active_pre_roll
            if not simulated_cursor and block_end is not None and ordinal < config.max_blocks - 1:
                schedule_now = _schedule_now(config.clock(), config.schedule_timezone)
                wait_seconds = max(0.0, (block_end - schedule_now).total_seconds())
                if wait_seconds > 0:
                    events.append(
                        {
                            "event": "block_wait_until_boundary",
                            "block_number": ordinal + 1,
                            "block": block_info,
                            "wait_seconds": wait_seconds,
                            "until": block_end.isoformat(),
                        }
                    )
                    _emit_live_status(
                        config,
                        status="running",
                        channel_output_dir=channel_output_dir,
                        events=events,
                        playout_state=playout_state,
                        schedule_now=config.clock(),
                        stale_schedule=last_stale_schedule,
                        ffmpeg=last_ffmpeg_status,
                    )
                    filler_duration = wait_seconds if active_pre_roll is not None else wait_seconds + BOUNDARY_FILLER_STARTUP_GUARD_SECONDS
                    filler_diagnostics = dict(
                        self.filler_runner.run(
                            output_dir=channel_output_dir,
                            output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                            duration=filler_duration,
                            hls_start_number=hls_start_number,
                            hls_start_time_offset=hls_start_time_offset,
                            hls_append=True,
                            stream_profile=config.stream_profile,
                        )
                    )
                    filler_playlist = Path(str(filler_diagnostics.get("playlist") or ""))
                    if filler_playlist.exists():
                        if config.stream_profile == "jellyfin":
                            _normalize_jellyfin_live_playlist(filler_playlist)
                            filler_diagnostics["hls_boundary_starts"] = []
                            filler_diagnostics["hls_discontinuity_sequence"] = 0
                        else:
                            filler_state = _rewrite_live_playlist_boundaries(filler_playlist, boundary_starts=())
                            filler_diagnostics["hls_boundary_starts"] = []
                            filler_diagnostics["hls_discontinuity_sequence"] = filler_state.get("discontinuity_sequence")
                    events.append(
                        {
                            "event": "block_filler",
                            "block_number": ordinal + 1,
                            "block": block_info,
                            "duration": filler_duration,
                            "wait_seconds": wait_seconds,
                            "startup_guard_seconds": BOUNDARY_FILLER_STARTUP_GUARD_SECONDS,
                            "until": block_end.isoformat(),
                            "status": filler_diagnostics.get("status"),
                            "playlist": filler_diagnostics.get("playlist"),
                            "hls_start_number": hls_start_number,
                            "hls_start_time_offset": hls_start_time_offset,
                            "hls_next_start_number": filler_diagnostics.get("hls_next_start_number"),
                            "hls_next_start_time_offset": filler_diagnostics.get("hls_next_start_time_offset"),
                            "ffmpeg_returncode": _ffmpeg_returncode(filler_diagnostics),
                        }
                    )
                    hls_start_number = int(filler_diagnostics.get("hls_next_start_number") or hls_start_number)
                    hls_start_time_offset = _next_hls_start_time_offset(filler_diagnostics, fallback=hls_start_time_offset)
                    if active_pre_roll is not None and active_pre_roll.get("next_index") == selected.index + 1:
                        gate = GatedJellyfinPreRoll(
                            public_playlist=channel_output_dir / f"{FFMpegHLSCommandBuilder._slug(config.channel)}.m3u8",
                            staged_playlist=active_pre_roll["stage_playlist"],
                            publish_at=active_pre_roll["next_start"],
                            public_next_segment_number=hls_start_number,
                            staged_block=str(active_pre_roll["next_block"].get("title") or "scheduled-block"),
                            minimum_ready_duration_seconds=config.jellyfin_pre_roll_min_buffer_seconds,
                        )
                        last_pre_roll_status = gate.publish_if_due(block_end)
                        events.append({"event": "public_boundary_switch" if last_pre_roll_status.get("pre_roll_state") == "published" else "pre_roll_fallback", **last_pre_roll_status})
                        if last_pre_roll_status.get("pre_roll_state") == "published":
                            staged_duration = _playlist_duration(active_pre_roll["stage_playlist"])
                            public_resume = {
                                "selected_index": active_pre_roll["next_index"],
                                "run_now": active_pre_roll["next_start"] + timedelta(seconds=staged_duration),
                                "hls_start_number": gate.public_next_segment_number,
                                "hls_start_time_offset": hls_start_time_offset + staged_duration,
                            }
                        active_pre_roll = None if last_pre_roll_status.get("pre_roll_state") != "held-for-buffer" else active_pre_roll
                    _emit_live_status(
                        config,
                        status="running",
                        channel_output_dir=channel_output_dir,
                        events=events,
                        playout_state=playout_state,
                        schedule_now=config.clock(),
                        stale_schedule=last_stale_schedule,
                        ffmpeg=last_ffmpeg_status,
                    )
            buffer_wait_started = time.monotonic()
            while (
                not simulated_cursor
                and active_pre_roll is not None
                and last_pre_roll_status is not None
                and last_pre_roll_status.get("pre_roll_state") == "held-for-buffer"
                and time.monotonic() - buffer_wait_started < config.jellyfin_pre_roll_max_publish_delay_seconds
            ):
                filler_diagnostics = dict(
                    self.filler_runner.run(
                        output_dir=channel_output_dir,
                        output_name=FFMpegHLSCommandBuilder._slug(config.channel),
                        duration=HLS_TARGET_SEGMENT_DURATION,
                        hls_start_number=hls_start_number,
                        hls_start_time_offset=hls_start_time_offset,
                        hls_append=True,
                        stream_profile="jellyfin",
                    )
                )
                hls_start_number = int(filler_diagnostics.get("hls_next_start_number") or hls_start_number)
                hls_start_time_offset = _next_hls_start_time_offset(filler_diagnostics, fallback=hls_start_time_offset)
                events.append(
                    {
                        "event": "boundary_buffer_filler",
                        "block_number": ordinal + 1,
                        "duration": HLS_TARGET_SEGMENT_DURATION,
                        "status": filler_diagnostics.get("status"),
                        "hls_start_number": hls_start_number,
                        "pre_roll": dict(last_pre_roll_status),
                    }
                )
                gate = GatedJellyfinPreRoll(
                    public_playlist=channel_output_dir / f"{FFMpegHLSCommandBuilder._slug(config.channel)}.m3u8",
                    staged_playlist=active_pre_roll["stage_playlist"],
                    publish_at=active_pre_roll["next_start"],
                    public_next_segment_number=hls_start_number,
                    staged_block=str(active_pre_roll["next_block"].get("title") or "scheduled-block"),
                    minimum_ready_duration_seconds=config.jellyfin_pre_roll_min_buffer_seconds,
                )
                last_pre_roll_status = gate.publish_if_due(_schedule_now(config.clock(), config.schedule_timezone))
                events.append({"event": "public_boundary_switch" if last_pre_roll_status.get("pre_roll_state") == "published" else "pre_roll_buffer_held", **last_pre_roll_status})
                if last_pre_roll_status.get("pre_roll_state") == "published":
                    staged_duration = _playlist_duration(active_pre_roll["stage_playlist"])
                    public_resume = {
                        "selected_index": active_pre_roll["next_index"],
                        "run_now": active_pre_roll["next_start"] + timedelta(seconds=staged_duration),
                        "hls_start_number": gate.public_next_segment_number,
                        "hls_start_time_offset": hls_start_time_offset + staged_duration,
                    }
                    active_pre_roll = None
            if active_pre_roll is not None and last_pre_roll_status is not None and last_pre_roll_status.get("pre_roll_state") == "held-for-buffer":
                last_pre_roll_status = {
                    **last_pre_roll_status,
                    "pre_roll_state": "fallback",
                    "fallback_reason": "safe-buffer-timeout",
                    "publish_timeout_seconds": config.jellyfin_pre_roll_max_publish_delay_seconds,
                }
                events.append({"event": "pre_roll_buffer_timeout", **last_pre_roll_status})
                active_pre_roll = None
            cursor = block_end or run_now or cursor
            previous_block_info = block_info

        summary = _events_plan_summary(events)
        result_schedule_now = _schedule_now(last_schedule_now, config.schedule_timezone) if last_schedule_now is not None else None
        result = {
            "status": "complete",
            "channel": config.channel,
            "channel_output_dir": str(channel_output_dir),
            "output_root": str(config.output_root),
            "max_blocks": config.max_blocks,
            "blocks_completed": config.max_blocks,
            "duration_limit": config.duration_limit,
            "stream_profile": config.stream_profile,
            "playout_mode": config.playout_mode,
            "schedule_timezone": config.schedule_timezone,
            "schedule_now": result_schedule_now.isoformat() if result_schedule_now is not None else None,
            **summary,
            "events": events,
            "stale_schedule": dict(last_stale_schedule or {}),
        }
        if last_playout_state is not None:
            result.update(last_playout_state.status_fields(schedule_now=result_schedule_now))
        result["hls"] = {
            "playlist": str(channel_output_dir / f"{FFMpegHLSCommandBuilder._slug(config.channel)}.m3u8"),
            "channel_output_dir": str(channel_output_dir),
            "output_root": str(config.output_root),
        }
        if last_ffmpeg_status is not None:
            result["ffmpeg"] = dict(last_ffmpeg_status)
        if last_pre_roll_status is not None:
            result["jellyfin_pre_roll"] = dict(last_pre_roll_status)
        if config.status_callback is not None:
            config.status_callback(result)
        return result


def _num(value: float) -> str:
    return (f"{value:.6f}").rstrip("0").rstrip(".")


def _count_hls_segments_from(playlist: Path, *, start_number: int) -> int:
    if not playlist.exists():
        return 0
    count = 0
    for line in playlist.read_text(errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        stem = Path(text).stem
        suffix = stem.rsplit("_", 1)[-1]
        if suffix.isdigit() and int(suffix) >= start_number:
            count += 1
    return count


def _block_run_failed(diagnostics: Mapping[str, Any]) -> bool:
    status = str(diagnostics.get("status") or "")
    if status and status not in {"ok", "dry-run"}:
        return True
    returncode = _ffmpeg_returncode(diagnostics)
    return isinstance(returncode, int) and returncode != 0


def _ffmpeg_returncode(diagnostics: Mapping[str, Any]) -> Any:
    raw_ffmpeg = diagnostics.get("ffmpeg")
    if isinstance(raw_ffmpeg, Mapping):
        return raw_ffmpeg.get("returncode")
    return None


def _finalize_block_run_diagnostics(
    diagnostics: Mapping[str, Any],
    *,
    simulated_cursor: bool,
    run_started_at: datetime,
    clock: Callable[[], datetime],
    duration_limit: float,
    schedule_timezone: str | None,
) -> dict[str, Any]:
    diagnostics_dict = dict(diagnostics)
    if simulated_cursor or _block_run_failed(diagnostics_dict):
        return diagnostics_dict
    if _block_run_completed_prematurely(
        diagnostics_dict,
        run_started_at=run_started_at,
        run_finished_at=clock(),
        duration_limit=duration_limit,
        schedule_timezone=schedule_timezone,
    ):
        diagnostics_dict["status"] = "premature-complete"
    return diagnostics_dict


def _block_run_completed_prematurely(
    diagnostics: Mapping[str, Any],
    *,
    run_started_at: datetime,
    run_finished_at: datetime,
    duration_limit: float,
    schedule_timezone: str | None,
) -> bool:
    if _block_run_failed(diagnostics):
        return False
    expected = max(0.0, duration_limit)
    tolerance = min(30.0, max(10.0, expected * 0.1))
    emitted = diagnostics.get("emitted_media_duration")
    if isinstance(emitted, (int, float)) and expected > 0 and emitted > 0 and emitted + tolerance >= expected:
        return False
    started = _schedule_now(run_started_at, schedule_timezone)
    finished = _schedule_now(run_finished_at, schedule_timezone)
    elapsed = max(0.0, (finished - started).total_seconds())
    return expected > 0 and elapsed > 0 and elapsed + tolerance < expected


def _next_hls_start_time_offset(diagnostics: Mapping[str, Any], *, fallback: float) -> float:
    raw_offset = diagnostics.get("hls_next_start_time_offset")
    if isinstance(raw_offset, (int, float)):
        return float(raw_offset)
    return fallback


def _playlist_duration(playlist: Path) -> float:
    """Return the accumulated EXTINF duration, independent of HLS sequence numbers."""
    try:
        lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0.0
    total = 0.0
    for line in lines:
        text = line.strip()
        if not text.startswith("#EXTINF:"):
            continue
        try:
            total += float(text.removeprefix("#EXTINF:").split(",", 1)[0])
        except ValueError:
            continue
    return total


def _is_show_to_show_boundary(previous_block: Mapping[str, Any], next_block: Mapping[str, Any]) -> bool:
    previous_type = _last_content_type(previous_block)
    next_type = _first_content_type(next_block)
    return _is_show_content(previous_type) and _is_show_content(next_type)


def _first_content_type(block: Mapping[str, Any]) -> str | None:
    plan = block.get("plan")
    if not isinstance(plan, list):
        return None
    for item in plan:
        if isinstance(item, Mapping):
            return _item_content_type(item)
    return None


def _last_content_type(block: Mapping[str, Any]) -> str | None:
    plan = block.get("plan")
    if not isinstance(plan, list):
        return None
    for item in reversed(plan):
        if isinstance(item, Mapping):
            return _item_content_type(item)
    return None


def _item_content_type(item: Mapping[str, Any]) -> str | None:
    value = item.get("content_type") or item.get("type") or item.get("fs42_type")
    return str(value).lower() if value is not None else None


def _is_show_content(content_type: str | None) -> bool:
    return content_type in {"feature", "show", "episode", "program", "programme"}


def _emit_live_status(
    config: LiveControllerConfig,
    *,
    status: str,
    channel_output_dir: Path,
    events: Sequence[Mapping[str, Any]],
    playout_state: SupervisedPlayout,
    schedule_now: datetime | None,
    stale_schedule: Mapping[str, Any] | None = None,
    ffmpeg: Mapping[str, Any] | None = None,
    pre_roll: Mapping[str, Any] | None = None,
) -> None:
    if config.status_callback is None:
        return
    local_schedule_now = _schedule_now(schedule_now, config.schedule_timezone) if schedule_now is not None else None
    payload = {
        "status": status,
        "channel": config.channel,
        "channel_output_dir": str(channel_output_dir),
        "output_root": str(config.output_root),
        "max_blocks": config.max_blocks,
        "duration_limit": config.duration_limit,
        "schedule_timezone": config.schedule_timezone,
        "stream_profile": config.stream_profile,
        "playout_mode": config.playout_mode,
        "schedule_now": local_schedule_now.isoformat() if local_schedule_now is not None else None,
        **playout_state.status_fields(schedule_now=local_schedule_now),
        "stale_schedule": dict(stale_schedule or {}),
        "stale_schedule": dict(stale_schedule or {}),
        "hls": {
            "playlist": str(channel_output_dir / f"{FFMpegHLSCommandBuilder._slug(config.channel)}.m3u8"),
            "channel_output_dir": str(channel_output_dir),
            "output_root": str(config.output_root),
        },
        "events": [dict(event) for event in events],
    }
    if ffmpeg is not None:
        payload["ffmpeg"] = dict(ffmpeg)
    if pre_roll is not None:
        payload["jellyfin_pre_roll"] = dict(pre_roll)
    config.status_callback(payload)


def clean_hls_outputs(directory: Path) -> list[Path]:
    """Remove stale HLS playlists/segments from a stable channel directory only."""

    directory.mkdir(parents=True, exist_ok=True)
    removed: list[Path] = []
    root = directory.resolve(strict=False)
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".m3u8", ".ts"} or not path.is_file():
            continue
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:  # defensive guard against unsafe traversal/symlink surprises
            raise ValueError(f"refusing to remove HLS output outside channel directory: {path}") from exc
        path.unlink()
        removed.append(path)
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded FS42 live block lifecycle controller.")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--output-root", type=Path, default=Path("/tmp/fs42stream-live"))
    parser.add_argument("--max-blocks", type=int, default=2)
    parser.add_argument("--duration-limit", type=float, default=10.0)
    parser.add_argument("--schedule-scheme", default=DEFAULT_SCHEDULE_SCHEME)
    parser.add_argument("--schedule-host", default=DEFAULT_SCHEDULE_HOST)
    parser.add_argument("--schedule-port", type=int, default=DEFAULT_SCHEDULE_PORT)
    parser.add_argument("--schedule-base-path", default=DEFAULT_SCHEDULE_BASE_PATH)
    parser.add_argument("--api-base-url", default="", help="deprecated full schedule API URL override; prefer --schedule-* options")
    parser.add_argument("--fs42-root", type=Path, default=Path(DEFAULT_FS42_ROOT))
    parser.add_argument("--sdtv-root", type=Path, default=Path(DEFAULT_SDTV_ROOT))
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE)
    parser.add_argument("--video-encoder", default="libx264", help="video encoder, e.g. libx264 or h264_vaapi")
    parser.add_argument("--vaapi-device", help="VAAPI device path, e.g. /dev/dri/renderD128")
    parser.add_argument("--schedule-timezone", default=DEFAULT_SCHEDULE_TIMEZONE, help="timezone for naive FS42 schedule timestamps, e.g. Europe/London")
    parser.add_argument("--max-recovery-attempts-per-block", type=int, default=3, help="bounded ffmpeg failure recovery attempts within a schedule block")
    parser.add_argument("--fallback-slate-video", type=Path)
    parser.add_argument("--brb-image-path", default=os.environ.get("FS42STREAM_BRB_IMAGE_PATH", str(DEFAULT_BRB_IMAGE_PATH)), help="fallback BRB image path relative to FS42 root or absolute under an allowed media root")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--now", help="override current time for deterministic tests, e.g. 2026-06-17T10:05:00")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stream-profile", choices=("direct", "jellyfin"), default="direct", help="HLS packaging profile; jellyfin writes under <channel>/jellyfin")
    parser.add_argument("--playout-mode", choices=("hls-primary", "ts-primary"), default="ts-primary", help="render directly to HLS or render TS first then package HLS")
    parser.add_argument("--hls-retention-max-age-seconds", type=float, default=6 * 60 * 60, help="rolling HLS segment retention age")
    parser.add_argument("--hls-retention-max-segments-per-dir", type=int, default=7200, help="maximum HLS .ts segments to keep in each direct/Jellyfin channel directory")
    parser.add_argument("--jellyfin-pre-roll-lead-seconds", type=float, default=float(os.environ.get("FS42STREAM_JELLYFIN_PRE_ROLL_LEAD_SECONDS", "0")), help="private Jellyfin next-block staging lead time; zero disables it")
    parser.add_argument("--jellyfin-pre-roll-min-buffer-seconds", type=float, default=float(os.environ.get("FS42STREAM_JELLYFIN_PRE_ROLL_MIN_BUFFER_SECONDS", "30")), help="minimum staged Jellyfin duration required before a public boundary switch")
    parser.add_argument("--jellyfin-pre-roll-max-publish-delay-seconds", type=float, default=float(os.environ.get("FS42STREAM_JELLYFIN_PRE_ROLL_MAX_PUBLISH_DELAY_SECONDS", "60")), help="maximum filler-backed delay while waiting for the staged Jellyfin safe buffer")
    args = parser.parse_args(argv)

    schedule_api_url = args.api_base_url or build_schedule_api_base_url(scheme=args.schedule_scheme, host=args.schedule_host, port=args.schedule_port, base_path=args.schedule_base_path)
    schedule_client = FS42ScheduleClient(schedule_api_url, timeout=args.timeout)
    block_runner = BlockRunner(
        client=FS42ScheduleClient(schedule_api_url, timeout=args.timeout),
        planner=BlockPlanner(PathResolver(fs42_root=args.fs42_root, sdtv_root=args.sdtv_root), FFProbe(args.ffprobe), fallback_slate_video=args.fallback_slate_video, brb_image_path=args.brb_image_path),
        builder=FFMpegHLSCommandBuilder(args.ffmpeg, video_encoder=args.video_encoder, vaapi_device=args.vaapi_device),
    )
    controller = LiveController(schedule_client=schedule_client, block_runner=block_runner)
    config = LiveControllerConfig(
        channel=args.channel,
        output_root=args.output_root,
        max_blocks=args.max_blocks,
        duration_limit=args.duration_limit,
        now=_parse_datetime(args.now) if args.now else None,
        dry_run=args.dry_run,
        schedule_timezone=args.schedule_timezone,
        max_recovery_attempts_per_block=args.max_recovery_attempts_per_block,
        stream_profile=args.stream_profile,
        playout_mode=args.playout_mode,
        hls_retention_max_age_seconds=args.hls_retention_max_age_seconds,
        hls_retention_max_segments_per_dir=args.hls_retention_max_segments_per_dir,
        brb_image_path=args.brb_image_path,
        jellyfin_pre_roll_lead_seconds=args.jellyfin_pre_roll_lead_seconds,
        jellyfin_pre_roll_min_buffer_seconds=args.jellyfin_pre_roll_min_buffer_seconds,
        jellyfin_pre_roll_max_publish_delay_seconds=args.jellyfin_pre_roll_max_publish_delay_seconds,
    )
    try:
        result = controller.run(config)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _complete_event(*, ordinal: int, block: Mapping[str, Any], diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    raw_ffmpeg = diagnostics.get("ffmpeg")
    raw_hls = diagnostics.get("hls")
    ffmpeg: Mapping[str, Any] = raw_ffmpeg if isinstance(raw_ffmpeg, Mapping) else {}
    hls: Mapping[str, Any] = raw_hls if isinstance(raw_hls, Mapping) else {}
    playlist = hls.get("playlist") or diagnostics.get("playlist")
    segments = hls.get("segments") or []
    event = {
        "event": "block_complete",
        "block_number": ordinal + 1,
        "block": dict(block),
        "status": diagnostics.get("status"),
        "playlist": playlist,
        "playlist_paths": {"playlist": playlist, "segments": segments},
        "ffmpeg_returncode": ffmpeg.get("returncode"),
        "runtime_fallback_diagnostics": _runtime_fallback_diagnostics(diagnostics),
        "catch_up": diagnostics.get("catch_up"),
    }
    for key in ("plan_item_count", "plan_item_counts", "commercial_count", "ad_count", "commercial_paths", "commercial_path_count"):
        if key in diagnostics:
            event[key] = diagnostics[key]
    return event


def _runtime_fallback_diagnostics(diagnostics: Mapping[str, Any]) -> list[str]:
    plan = diagnostics.get("plan")
    if not isinstance(plan, list):
        return []
    messages: list[str] = []
    for item in plan:
        if not isinstance(item, Mapping):
            continue
        if item.get("runtime_action"):
            diagnostic = item.get("diagnostic") or item.get("runtime_action")
            messages.append(str(diagnostic))
    return messages


def _events_plan_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    total_items = 0
    commercial_count = 0
    commercial_paths: list[str] = []
    for event in events:
        if event.get("event") != "block_complete":
            continue
        raw_count = event.get("plan_item_count")
        if isinstance(raw_count, int):
            total_items += raw_count
        raw_counts = event.get("plan_item_counts")
        if isinstance(raw_counts, Mapping):
            for key, value in raw_counts.items():
                if isinstance(value, int):
                    item_type = str(key)
                    counts[item_type] = counts.get(item_type, 0) + value
        raw_commercial_count = event.get("commercial_count")
        if isinstance(raw_commercial_count, int):
            commercial_count += raw_commercial_count
        raw_paths = event.get("commercial_paths")
        if isinstance(raw_paths, list):
            commercial_paths.extend(str(path) for path in raw_paths)
    return {
        "plan_item_count": total_items,
        "plan_item_counts": counts,
        "commercial_count": commercial_count,
        "ad_count": commercial_count,
        "commercial_paths": commercial_paths,
        "commercial_path_count": len(commercial_paths),
    }


if __name__ == "__main__":
    raise SystemExit(main())
