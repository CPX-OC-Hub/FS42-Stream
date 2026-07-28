from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping


DEFAULT_SCHEDULE_SCHEME = "http"
DEFAULT_SCHEDULE_HOST = "127.0.0.1"
DEFAULT_SCHEDULE_PORT = 4242
DEFAULT_SCHEDULE_BASE_PATH = ""
DEFAULT_SCHEDULE_API_BASE_URL = "http://127.0.0.1:4242"
DEFAULT_SCHEDULE_CHANNEL = "Example Channel"


def build_schedule_api_base_url(
    *,
    scheme: str = DEFAULT_SCHEDULE_SCHEME,
    host: str = DEFAULT_SCHEDULE_HOST,
    port: int | str | None = DEFAULT_SCHEDULE_PORT,
    base_path: str = DEFAULT_SCHEDULE_BASE_PATH,
) -> str:
    """Build the FieldStation42 schedule API URL from split config values."""

    normalized_scheme = (scheme or DEFAULT_SCHEDULE_SCHEME).rstrip(":/")
    normalized_host = (host or DEFAULT_SCHEDULE_HOST).strip().strip("/")
    normalized_path = "/" + base_path.strip("/") if base_path and base_path.strip("/") else ""
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    if port in (None, ""):
        return f"{normalized_scheme}://{normalized_host}{normalized_path}"
    return f"{normalized_scheme}://{normalized_host}:{int(port)}{normalized_path}"


class FS42ScheduleClient:
    """Small stdlib client for the FieldStation42 schedule API."""

    def __init__(
        self,
        base_url: str = DEFAULT_SCHEDULE_API_BASE_URL,
        *,
        timeout: float = 10.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def fetch_schedule(self, channel: str = DEFAULT_SCHEDULE_CHANNEL, *, expected_blocks: int | None = None) -> dict[str, Any]:
        """Fetch `/schedules/{channel}` and optionally verify block count.

        Callers may provide `expected_blocks` when an installation has a fixed
        schedule size, otherwise no site-specific block-count guard is applied.
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


    def fetch_schedule_summary(self, channel: str = DEFAULT_SCHEDULE_CHANNEL) -> Mapping[str, Any]:
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
