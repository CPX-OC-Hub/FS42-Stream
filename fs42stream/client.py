from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping


class FS42ScheduleClient:
    """Small stdlib client for the FieldStation42 schedule API."""

    def __init__(
        self,
        base_url: str = "http://192.168.10.252:4242",
        *,
        timeout: float = 10.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def fetch_schedule(self, channel: str = "Sky One", *, expected_blocks: int | None = 338) -> dict[str, Any]:
        """Fetch `/schedules/{channel}` and optionally verify block count.

        The live Sky One schedule is expected to contain 338 blocks for Phase 1,
        but callers/tests can pass `expected_blocks=None` to disable the guard.
        """

        encoded = urllib.parse.quote(channel, safe="")
        request = urllib.request.Request(
            f"{self.base_url}/schedules/{encoded}",
            headers={"Accept": "application/json", "User-Agent": "fs42stream-phase1/0"},
        )
        with self._opener(request, timeout=self.timeout) as response:
            payload = response.read()
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("schedule response must be a JSON object")
        blocks = data.get("schedule_blocks")
        if not isinstance(blocks, list):
            raise ValueError("schedule response missing schedule_blocks list")
        if expected_blocks is not None and len(blocks) != expected_blocks:
            raise ValueError(f"expected {expected_blocks} schedule blocks, got {len(blocks)}")
        return data

    def fetch_summary(self) -> Mapping[str, Any]:
        request = urllib.request.Request(f"{self.base_url}/summary", headers={"Accept": "application/json"})
        with self._opener(request, timeout=self.timeout) as response:
            payload = response.read()
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("summary response must be a JSON object")
        return data


    def fetch_schedule_summary(self, channel: str = "Sky One") -> Mapping[str, Any]:
        encoded = urllib.parse.quote(channel, safe="")
        request = urllib.request.Request(
            f"{self.base_url}/summary/schedules/{encoded}",
            headers={"Accept": "application/json", "User-Agent": "fs42stream-phase1/0"},
        )
        with self._opener(request, timeout=self.timeout) as response:
            payload = response.read()
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("schedule summary response must be a JSON object")
        return data
