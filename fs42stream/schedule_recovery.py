from __future__ import annotations

import threading
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo


class ScheduleEndpoint(Protocol):
    base_url: str

    def fetch_schedule(self, channel: str, *, expected_blocks: int | None = None) -> Mapping[str, Any]: ...
    def fetch_schedule_summary(self, channel: str) -> Mapping[str, Any]: ...


class RecoveringScheduleClient:
    """Keep playout on a usable full schedule while reconciling stale summaries.

    FieldStation42's compact summary can lag behind its full schedule endpoint.
    A current full schedule is authoritative for the current block, so it is
    retained while a bounded ``reconcile_schedule`` request asks FS42 to rebuild
    its summary. A missing or expired summary *and* a schedule with no current
    block still requires the existing BRB safety fallback.
    """

    def __init__(
        self,
        *,
        primary: ScheduleEndpoint,
        fallback: ScheduleEndpoint | None = None,
        recovery_interval: float = 30.0,
        schedule_timezone: str | None = "Europe/London",
        clock: Callable[[], datetime] | None = None,
        reconciliation_cooldown: float = 300.0,
    ) -> None:
        if recovery_interval <= 0:
            raise ValueError("recovery_interval must be positive")
        if reconciliation_cooldown <= 0:
            raise ValueError("reconciliation_cooldown must be positive")
        self.primary = primary
        self.fallback = fallback
        self.recovery_interval = recovery_interval
        self.schedule_timezone = schedule_timezone
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reconciliation_cooldown = reconciliation_cooldown
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._channel: str | None = None
        self._active = False
        self._source = "primary"
        self._reason: str | None = None
        self._last_error: str | None = None
        self._last_primary_success_at: str | None = None
        self._last_recheck_at: str | None = None
        self._primary_ready_schedule: dict[str, Any] | None = None
        self._primary_ready_summary: dict[str, Any] | None = None
        self._primary_ready_summary_error: BaseException | None = None
        self._current_summary: dict[str, Any] | None = None
        self._cached_schedule: dict[str, Any] | None = None
        self._full_schedule_current: bool | None = None
        self._requires_placeholder = False
        self._last_reconciliation_at: dict[str, float] = {}
        self._reconciliation_attempts = 0
        self._last_reconciliation_result: Mapping[str, Any] | None = None

    def fetch_schedule(self, channel: str, *, expected_blocks: int | None = None) -> Mapping[str, Any]:
        self._remember_channel(channel)
        with self._lock:
            ready_schedule = self._primary_ready_schedule
            if self._active and ready_schedule is not None:
                self._activate_primary(ready_schedule, self._primary_ready_summary)
                self._apply_schedule_health(
                    channel,
                    ready_schedule,
                    self._primary_ready_summary or {},
                    summary_error=self._primary_ready_summary_error,
                )
                self._primary_ready_schedule = None
                self._primary_ready_summary = None
                self._primary_ready_summary_error = None
                return dict(ready_schedule)

        try:
            schedule, summary, summary_error = self._fetch_primary(channel, expected_blocks=expected_blocks)
        except Exception as exc:
            self._activate_fallback(channel, exc)
            return self._fetch_fallback_or_cache(channel, expected_blocks=expected_blocks)

        if summary_error is not None and not _full_schedule_covers_now(
            schedule, now=self.clock(), schedule_timezone=self.schedule_timezone
        ):
            self._activate_summary_unavailable_fallback(channel, summary_error)
            return self._fetch_fallback_or_cache(channel, expected_blocks=expected_blocks)

        with self._lock:
            self._activate_primary(schedule, summary)
            self._apply_schedule_health(channel, schedule, summary, summary_error=summary_error)
        return dict(schedule)

    def fetch_schedule_summary(self, channel: str) -> Mapping[str, Any]:
        self._remember_channel(channel)
        with self._lock:
            if self._current_summary is not None:
                return dict(self._current_summary)
        self.fetch_schedule(channel)
        with self._lock:
            return dict(self._current_summary or {})

    def recheck_upstream(self, channel: str | None = None) -> bool:
        target_channel = channel or self._channel
        if not target_channel:
            return False
        try:
            schedule, summary, summary_error = self._fetch_primary(target_channel, expected_blocks=None)
        except Exception as exc:
            with self._lock:
                self._last_recheck_at = _utc_now()
                self._last_error = _error_text(exc)
            return False
        if summary_error is not None and not _full_schedule_covers_now(
            schedule, now=self.clock(), schedule_timezone=self.schedule_timezone
        ):
            with self._lock:
                self._last_recheck_at = _utc_now()
                self._last_error = _error_text(summary_error)
            return False
        with self._lock:
            self._last_recheck_at = _utc_now()
            self._last_primary_success_at = self._last_recheck_at
            self._primary_ready_schedule = dict(schedule)
            self._primary_ready_summary = dict(summary)
            self._primary_ready_summary_error = summary_error
        return True

    def start(self, channel: str) -> None:
        self._remember_channel(channel)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._recheck_loop, name="fs42stream-upstream-recovery", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.recovery_interval + 1.0))

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "source": self._source,
                "reason": self._reason,
                "upstream": getattr(self.primary, "base_url", ""),
                "fallback": getattr(self.fallback, "base_url", "") if self.fallback is not None else None,
                "last_error": self._last_error,
                "last_primary_success_at": self._last_primary_success_at,
                "last_recheck_at": self._last_recheck_at,
                "recovery_interval_seconds": self.recovery_interval,
                "full_schedule_current": self._full_schedule_current,
                "requires_placeholder": self._requires_placeholder,
                "reconciliation_attempts": self._reconciliation_attempts,
                "last_reconciliation_result": dict(self._last_reconciliation_result or {}),
            }

    def _fetch_primary(
        self, channel: str, *, expected_blocks: int | None
    ) -> tuple[dict[str, Any], dict[str, Any], BaseException | None]:
        summary_error: BaseException | None = None
        try:
            summary = dict(self.primary.fetch_schedule_summary(channel))
        except Exception as exc:
            summary = {}
            summary_error = exc
        schedule = dict(self.primary.fetch_schedule(channel, expected_blocks=expected_blocks))
        now = _utc_now()
        with self._lock:
            self._last_primary_success_at = now
            self._last_recheck_at = now
        return schedule, summary, summary_error

    def _apply_schedule_health(
        self,
        channel: str,
        schedule: Mapping[str, Any],
        summary: Mapping[str, Any],
        *,
        summary_error: BaseException | None = None,
    ) -> None:
        full_schedule_current = _full_schedule_covers_now(schedule, now=self.clock(), schedule_timezone=self.schedule_timezone)
        summary_fresh = _summary_is_fresh(summary, channel=channel, now=self.clock(), schedule_timezone=self.schedule_timezone)
        self._full_schedule_current = full_schedule_current
        self._requires_placeholder = not full_schedule_current
        if summary_error is not None:
            self._active = True
            self._source = "primary"
            self._reason = "summary-unavailable-full-schedule-valid"
            self._last_error = _error_text(summary_error)
            self._attempt_reconciliation(channel)
            return
        if summary_fresh:
            return
        if not full_schedule_current:
            self._active = True
            self._source = "primary"
            self._reason = "summary-stale-no-current-full-schedule"
            return
        self._active = True
        self._source = "primary"
        self._reason = "summary-stale-full-schedule-valid"
        self._attempt_reconciliation(channel)

    def _attempt_reconciliation(self, channel: str) -> None:
        now = monotonic()
        previous = self._last_reconciliation_at.get(channel)
        if previous is not None and now - previous < self.reconciliation_cooldown:
            return
        self._last_reconciliation_at[channel] = now
        self._reconciliation_attempts += 1
        reconcile = getattr(self.primary, "reconcile_schedule", None)
        if not callable(reconcile):
            self._last_error = "primary client does not support schedule reconciliation"
            return
        try:
            result = reconcile(channel)
        except Exception as exc:
            self._last_error = _error_text(exc)
            self._last_reconciliation_result = {"status": "failed", "error": self._last_error}
            return
        self._last_reconciliation_result = dict(result) if isinstance(result, Mapping) else {"status": "requested"}

    def _activate_primary(self, schedule: Mapping[str, Any], summary: Mapping[str, Any] | None) -> None:
        self._active = False
        self._source = "primary"
        self._reason = None
        self._last_error = None
        self._cached_schedule = dict(schedule)
        self._current_summary = dict(summary or {})

    def _activate_fallback(self, channel: str, exc: BaseException) -> None:
        with self._lock:
            self._active = True
            self._source = "fallback" if self.fallback is not None else "cache"
            self._reason = "upstream-unavailable"
            self._last_error = _error_text(exc)
            self._last_recheck_at = _utc_now()
            self._full_schedule_current = None
            self._requires_placeholder = False
        self.start(channel)

    def _activate_summary_unavailable_fallback(self, channel: str, exc: BaseException) -> None:
        with self._lock:
            self._active = True
            self._source = "fallback" if self.fallback is not None else "cache"
            self._reason = "summary-unavailable-no-current-full-schedule"
            self._last_error = _error_text(exc)
            self._last_recheck_at = _utc_now()
            self._full_schedule_current = False
            self._requires_placeholder = True
        self.start(channel)

    def _fetch_fallback_or_cache(self, channel: str, *, expected_blocks: int | None) -> Mapping[str, Any]:
        if self.fallback is not None:
            try:
                schedule = dict(self.fallback.fetch_schedule(channel, expected_blocks=expected_blocks))
                try:
                    summary = dict(self.fallback.fetch_schedule_summary(channel))
                except Exception:
                    summary = {}
                with self._lock:
                    self._source = "fallback"
                    self._cached_schedule = dict(schedule)
                    self._current_summary = summary
                return schedule
            except Exception as fallback_exc:
                with self._lock:
                    self._last_error = f"{self._last_error}; fallback: {_error_text(fallback_exc)}"
        with self._lock:
            if self._cached_schedule is not None:
                self._source = "cache"
                return dict(self._cached_schedule)
        raise RuntimeError("upstream schedule unavailable and no fallback or cached schedule is available")

    def _remember_channel(self, channel: str) -> None:
        with self._lock:
            self._channel = channel

    def _recheck_loop(self) -> None:
        while not self._stop_event.wait(self.recovery_interval):
            with self._lock:
                active = self._active
                channel = self._channel
            if active and channel:
                self.recheck_upstream(channel)


def _summary_is_fresh(summary: Mapping[str, Any], *, channel: str, now: datetime, schedule_timezone: str | None) -> bool:
    end = _summary_end(summary, channel)
    if end is None:
        return False
    comparable_now, comparable_end = _comparable_datetimes(now, end, schedule_timezone)
    return comparable_now <= comparable_end


def _summary_end(summary: Mapping[str, Any], channel: str) -> datetime | None:
    single = summary.get("schedule_summary")
    if isinstance(single, Mapping):
        return _parse_datetime(single.get("end"))
    many = summary.get("schedule_summaries")
    if isinstance(many, list):
        for item in many:
            if isinstance(item, Mapping) and str(item.get("network_id") or item.get("network_name") or "") == channel:
                return _parse_datetime(item.get("end"))
    return None


def _full_schedule_covers_now(schedule: Mapping[str, Any], *, now: datetime, schedule_timezone: str | None) -> bool:
    blocks = schedule.get("schedule_blocks")
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        start = _parse_datetime(block.get("start_time"))
        end = _parse_datetime(block.get("end_time"))
        if start is None or end is None:
            continue
        comparable_now, comparable_start = _comparable_datetimes(now, start, schedule_timezone)
        _ignored, comparable_end = _comparable_datetimes(now, end, schedule_timezone)
        if comparable_start <= comparable_now < comparable_end:
            return True
    return False


def _comparable_datetimes(now: datetime, value: datetime, schedule_timezone: str | None) -> tuple[datetime, datetime]:
    if value.tzinfo is None:
        if now.tzinfo is None:
            return now, value
        if schedule_timezone:
            local_now = now.astimezone(ZoneInfo(schedule_timezone))
            return local_now, value.replace(tzinfo=local_now.tzinfo)
        return now.replace(tzinfo=None), value
    if now.tzinfo is None:
        return now.replace(tzinfo=value.tzinfo), value
    return now.astimezone(value.tzinfo), value


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
