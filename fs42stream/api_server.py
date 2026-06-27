from __future__ import annotations

import argparse
import json
import mimetypes
import posixpath
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence, cast
from urllib.parse import quote, unquote, urlsplit


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_OUTPUT_ROOT = Path("/tmp/fs42stream-live")
DEFAULT_CHANNEL_NAME = "Sky One"
DEFAULT_CHANNEL_SLUG = "Sky_One"


@dataclass(frozen=True)
class APIServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    output_root: Path = DEFAULT_OUTPUT_ROOT
    status_json: Path | None = None


class FS42APIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler], config: APIServerConfig):
        super().__init__(server_address, RequestHandlerClass)
        self.config = config
        self.output_root = config.output_root.resolve(strict=False)
        self.status_json = (config.status_json or (config.output_root / "status.json")).resolve(strict=False)


class FS42APIRequestHandler(BaseHTTPRequestHandler):
    def _api_server(self) -> FS42APIHTTPServer:
        return cast(FS42APIHTTPServer, self.server)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        request_path = parsed.path
        if request_path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "fs42stream-api",
                    "channel": DEFAULT_CHANNEL_NAME,
                    "output_root": str(self._api_server().output_root),
                },
            )
            return

        if request_path == "/api/channels":
            channel = _channel_metadata()
            self._send_json(
                HTTPStatus.OK,
                {
                    "channels": [
                        {
                            **channel,
                            "status_url": f"/api/channels/{DEFAULT_CHANNEL_SLUG}/status",
                            "schedule_url": f"/api/channels/{DEFAULT_CHANNEL_SLUG}/schedule",
                            "runtime_url": f"/api/channels/{DEFAULT_CHANNEL_SLUG}/runtime",
                            "health_url": f"/api/channels/{DEFAULT_CHANNEL_SLUG}/health",
                            "events_url": f"/api/channels/{DEFAULT_CHANNEL_SLUG}/events",
                            "epg_url": f"/api/channels/{DEFAULT_CHANNEL_SLUG}/epg",
                            "hls_url": f"/hls/{DEFAULT_CHANNEL_SLUG}/",
                            "hls_playlist_url": f"/hls/{DEFAULT_CHANNEL_SLUG}/{DEFAULT_CHANNEL_SLUG}.m3u8",
                        }
                    ]
                },
            )
            return

        if request_path.startswith("/api/channels/") and request_path.endswith("/status"):
            self._handle_channel_status(request_path)
            return

        if request_path.startswith("/api/channels/") and request_path.endswith("/schedule"):
            self._handle_channel_schedule(request_path)
            return

        if request_path.startswith("/api/channels/") and request_path.endswith("/runtime"):
            self._handle_channel_runtime(request_path)
            return

        if request_path.startswith("/api/channels/") and request_path.endswith("/health"):
            self._handle_channel_health(request_path)
            return

        if request_path.startswith("/api/channels/") and request_path.endswith("/events"):
            self._handle_channel_events(request_path)
            return

        if request_path.startswith("/api/channels/") and request_path.endswith("/epg"):
            self._handle_channel_epg(request_path)
            return

        if request_path == "/iptv/channels.m3u":
            self._handle_iptv_channels_m3u()
            return

        iptv_channel_prefix = "/iptv/channels/"
        if request_path.startswith(iptv_channel_prefix) and request_path.endswith(".m3u"):
            raw_slug = request_path[len(iptv_channel_prefix) : -len(".m3u")]
            self._handle_iptv_channels_m3u(single_slug=unquote(raw_slug))
            return

        if request_path == "/iptv/xmltv.xml":
            self._handle_iptv_xmltv()
            return

        if request_path.startswith("/hls/"):
            self._handle_hls(request_path)
            return

        self._send_error(HTTPStatus.NOT_FOUND, "not_found", f"no route for {request_path}")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "only GET is supported")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "only GET is supported")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "only GET is supported")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "only GET is supported")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_channel_status(self, request_path: str) -> None:
        channel_slug = self._validated_channel_slug(request_path, expected_leaf="status")
        if channel_slug is None:
            return
        payload = self._read_status_payload()
        if payload is None:
            return
        self._send_json(HTTPStatus.OK, payload)

    def _handle_channel_schedule(self, request_path: str) -> None:
        channel_slug = self._validated_channel_slug(request_path, expected_leaf="schedule")
        if channel_slug is None:
            return
        status_payload = self._read_status_payload()
        if status_payload is None:
            return
        self._send_json(HTTPStatus.OK, _derived_schedule_payload(status_payload, channel_slug=channel_slug))

    def _handle_channel_runtime(self, request_path: str) -> None:
        channel_slug = self._validated_channel_slug(request_path, expected_leaf="runtime")
        if channel_slug is None:
            return
        status_payload = self._read_status_payload()
        if status_payload is None:
            return
        self._send_json(HTTPStatus.OK, _runtime_payload(status_payload, channel_slug=channel_slug, output_root=self._api_server().output_root))

    def _handle_channel_health(self, request_path: str) -> None:
        channel_slug = self._validated_channel_slug(request_path, expected_leaf="health")
        if channel_slug is None:
            return
        status_payload = self._read_status_payload()
        if status_payload is None:
            return
        runtime = _runtime_payload(status_payload, channel_slug=channel_slug, output_root=self._api_server().output_root)
        self._send_json(HTTPStatus.OK, _health_payload(runtime))

    def _handle_channel_events(self, request_path: str) -> None:
        channel_slug = self._validated_channel_slug(request_path, expected_leaf="events")
        if channel_slug is None:
            return
        status_payload = self._read_status_payload()
        if status_payload is None:
            return
        events = _event_list(status_payload)[-10:]
        self._send_json(HTTPStatus.OK, {"channel": _channel_metadata(channel_slug=channel_slug), "events": events})

    def _handle_channel_epg(self, request_path: str) -> None:
        channel_slug = self._validated_channel_slug(request_path, expected_leaf="epg")
        if channel_slug is None:
            return
        status_payload = self._read_status_payload()
        if status_payload is None:
            return
        self._send_json(HTTPStatus.OK, _epg_payload(status_payload, channel_slug=channel_slug))

    def _handle_iptv_channels_m3u(self, *, single_slug: str | None = None) -> None:
        if single_slug is not None and single_slug != DEFAULT_CHANNEL_SLUG:
            self._send_error(HTTPStatus.NOT_FOUND, "channel_not_found", f"unknown channel: {single_slug}")
            return
        body = _m3u_payload(base_url=self._request_base_url()).encode("utf-8")
        self._send_bytes(HTTPStatus.OK, body, "application/vnd.apple.mpegurl")

    def _handle_iptv_xmltv(self) -> None:
        status_payload = self._read_status_payload()
        if status_payload is None:
            return
        body = _xmltv_payload(status_payload).encode("utf-8")
        self._send_bytes(HTTPStatus.OK, body, "application/xml; charset=utf-8")

    def _request_base_url(self) -> str:
        host = self.headers.get("Host")
        if not host:
            address_host, address_port = self.server.server_address[:2]
            host = f"{address_host}:{address_port}"
        return f"http://{host}"

    def _validated_channel_slug(self, request_path: str, *, expected_leaf: str) -> str | None:
        parts = request_path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "channels"] or parts[3] != expected_leaf:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", f"no route for {request_path}")
            return None
        channel_slug = unquote(parts[2])
        if channel_slug != DEFAULT_CHANNEL_SLUG:
            self._send_error(HTTPStatus.NOT_FOUND, "channel_not_found", f"unknown channel: {channel_slug}")
            return None
        return channel_slug

    def _read_status_payload(self) -> Mapping[str, Any] | None:
        status_path = self._api_server().status_json
        if not status_path.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "status_not_found", f"status JSON does not exist: {status_path}")
            return None
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self._send_error(HTTPStatus.BAD_GATEWAY, "invalid_status_json", f"status JSON is invalid: {exc.msg}")
            return None
        except OSError as exc:
            self._send_error(HTTPStatus.BAD_GATEWAY, "status_unreadable", str(exc))
            return None
        if not isinstance(payload, Mapping):
            self._send_error(HTTPStatus.BAD_GATEWAY, "invalid_status_json", "status JSON must be an object")
            return None
        return payload

    def _handle_hls(self, request_path: str) -> None:
        prefix = f"/hls/{DEFAULT_CHANNEL_SLUG}/"
        if request_path == f"/hls/{DEFAULT_CHANNEL_SLUG}":
            self._send_error(HTTPStatus.BAD_REQUEST, "unsafe_hls_path", "HLS file path is required")
            return
        if not request_path.startswith(prefix):
            slug = unquote(request_path.removeprefix("/hls/").split("/", 1)[0])
            self._send_error(HTTPStatus.NOT_FOUND, "channel_not_found", f"unknown channel: {slug}")
            return

        raw_relative = request_path[len(prefix) :]
        safe_relative = _safe_hls_relative_path(raw_relative)
        if safe_relative is None:
            self._send_error(HTTPStatus.BAD_REQUEST, "unsafe_hls_path", "HLS path must stay under the channel output directory")
            return

        channel_root = (self._api_server().output_root / DEFAULT_CHANNEL_SLUG).resolve(strict=False)
        candidate = (channel_root / safe_relative).resolve(strict=False)
        try:
            candidate.relative_to(channel_root)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "unsafe_hls_path", "HLS path resolves outside the channel output directory")
            return

        if candidate.suffix not in {".m3u8", ".ts"}:
            self._send_error(HTTPStatus.NOT_FOUND, "hls_not_found", "only .m3u8 playlists and .ts segments are served")
            return
        if not candidate.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "hls_not_found", f"HLS file not found: {safe_relative}")
            return

        try:
            body = candidate.read_bytes()
        except OSError as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "hls_unreadable", str(exc))
            return

        content_type = _hls_content_type(candidate)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, Any] | list[Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self._send_bytes(status, body, "application/json")

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, error: str, message: str) -> None:
        self._send_json(status, {"error": error, "message": message, "status": int(status)})


def _safe_hls_relative_path(raw_relative: str) -> Path | None:
    if not raw_relative or raw_relative.startswith("/") or "//" in raw_relative:
        return None
    decoded = unquote(raw_relative)
    if decoded.startswith("/") or "\x00" in decoded or "//" in decoded:
        return None
    normalized = posixpath.normpath(decoded)
    if normalized in {".", ""}:
        return None
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    if normalized != decoded:
        return None
    return Path(*pure.parts)


def _channel_metadata(*, channel_slug: str = DEFAULT_CHANNEL_SLUG, name: str = DEFAULT_CHANNEL_NAME) -> dict[str, str]:
    return {"id": _channel_id(channel_slug), "slug": channel_slug, "name": name}


def _channel_id(channel_slug: str) -> str:
    normalized = "_".join(part for part in channel_slug.strip().split() if part) or channel_slug
    normalized = normalized.replace("-", "_").lower()
    return f"fs42.{normalized}"


def _runtime_payload(status_payload: Mapping[str, Any], *, channel_slug: str, output_root: Path) -> dict[str, Any]:
    schedule = _derived_schedule_payload(status_payload, channel_slug=channel_slug)
    channel_name = str(status_payload.get("channel") or DEFAULT_CHANNEL_NAME)
    schedule_now = schedule.get("schedule_now") or status_payload.get("updated_at")
    now_dt = _parse_iso_datetime(schedule_now)
    active_block = schedule.get("active_block") if isinstance(schedule.get("active_block"), Mapping) else None
    current_block = _runtime_block(active_block, now_dt=now_dt) if active_block is not None else None
    upcoming = schedule.get("upcoming_blocks")
    next_block = dict(upcoming[0]) if isinstance(upcoming, list) and upcoming and isinstance(upcoming[0], Mapping) else None
    current_item = schedule.get("current_plan_item") if isinstance(schedule.get("current_plan_item"), Mapping) else None
    if current_item is not None:
        current_item = _runtime_item(current_item, now_dt=now_dt)
    events = _event_list(status_payload)
    last_event = events[-1] if events else {}
    ffmpeg = _ffmpeg_payload(status_payload, events)
    hls = _inspect_hls_playlist(
        _playlist_path(status_payload, output_root=output_root, channel_slug=channel_slug),
        output_root=output_root,
        channel_slug=channel_slug,
    )
    return {
        "channel": _channel_metadata(channel_slug=channel_slug, name=channel_name),
        "service": {
            "status": status_payload.get("status"),
            "updated_at": status_payload.get("updated_at"),
            "schedule_now": schedule_now,
        },
        "block": {"current": current_block, "next": next_block},
        "item": {"current": current_item},
        "transition": {
            "last_reason": last_event.get("reason") or last_event.get("event"),
            "last_event_at": last_event.get("at") or last_event.get("recover_at") or last_event.get("until"),
            "recent_events": events[-10:],
        },
        "ffmpeg": ffmpeg,
        "hls": hls,
    }


def _runtime_block(block: Mapping[str, Any], *, now_dt: datetime | None) -> dict[str, Any]:
    result = dict(block)
    end = _parse_iso_datetime(block.get("end_time"))
    result["seconds_remaining"] = _seconds_until(end, now_dt)
    return result


def _runtime_item(item: Mapping[str, Any], *, now_dt: datetime | None) -> dict[str, Any]:
    result = dict(item)
    end = _parse_iso_datetime(item.get("wallclock_end"))
    result["seconds_remaining"] = _seconds_until(end, now_dt)
    return result


def _seconds_until(end: datetime | None, now_dt: datetime | None) -> float | None:
    if end is None or now_dt is None:
        return None
    return max(0.0, (end - now_dt).total_seconds())


def _event_list(status_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    events = status_payload.get("events")
    return [dict(event) for event in events if isinstance(event, Mapping)] if isinstance(events, list) else []


def _ffmpeg_payload(status_payload: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    raw = status_payload.get("ffmpeg")
    if isinstance(raw, Mapping):
        return {
            "state": raw.get("state") or ("running" if raw.get("pid") else "unknown"),
            "pid": raw.get("pid"),
            "started_at": raw.get("started_at"),
            "last_exit_code": raw.get("last_exit_code") if "last_exit_code" in raw else raw.get("returncode"),
            "last_error": raw.get("last_error"),
        }
    for event in reversed(events):
        if "ffmpeg_returncode" in event:
            return {"state": "exited", "pid": None, "started_at": None, "last_exit_code": event.get("ffmpeg_returncode"), "last_error": None}
    return {"state": "unknown", "pid": None, "started_at": None, "last_exit_code": None, "last_error": None}


def _playlist_path(status_payload: Mapping[str, Any], *, output_root: Path, channel_slug: str) -> Path:
    raw_hls = status_payload.get("hls")
    if isinstance(raw_hls, Mapping) and raw_hls.get("playlist"):
        return Path(str(raw_hls["playlist"]))
    if status_payload.get("playlist"):
        return Path(str(status_payload["playlist"]))
    return output_root / channel_slug / f"{channel_slug}.m3u8"


def _inspect_hls_playlist(playlist: Path, *, output_root: Path, channel_slug: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "playlist_url": _hls_playlist_url(playlist, output_root=output_root, channel_slug=channel_slug),
        "playlist_path": str(playlist),
        "media_sequence": None,
        "target_duration": None,
        "segment_count": 0,
        "last_segment_name": None,
        "last_segment_mtime": None,
        "seconds_since_last_segment": None,
        "freshness": "missing",
        "endlist": False,
    }
    if not playlist.is_file():
        return result
    try:
        lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        result["freshness"] = "unreadable"
        return result
    segments: list[str] = []
    for line in lines:
        text = line.strip()
        if text.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            result["media_sequence"] = _int_or_none(text.split(":", 1)[1])
        elif text.startswith("#EXT-X-TARGETDURATION:"):
            result["target_duration"] = _int_or_none(text.split(":", 1)[1])
        elif text == "#EXT-X-ENDLIST":
            result["endlist"] = True
        elif text and not text.startswith("#"):
            segments.append(text)
    result["segment_count"] = len(segments)
    if not segments:
        result["freshness"] = "stale"
        return result
    last_segment = segments[-1]
    result["last_segment_name"] = Path(last_segment).name
    segment_path = (playlist.parent / last_segment).resolve(strict=False)
    try:
        mtime = datetime.fromtimestamp(segment_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        result["freshness"] = "stale"
        return result
    now = datetime.now(timezone.utc)
    seconds_since = max(0.0, (now - mtime).total_seconds())
    result["last_segment_mtime"] = mtime.isoformat()
    result["seconds_since_last_segment"] = seconds_since
    target_duration = result.get("target_duration") if isinstance(result.get("target_duration"), int) else 2
    fresh_threshold = max(float(target_duration) * 3, 10.0)
    stale_threshold = max(float(target_duration) * 10, 60.0)
    if result.get("endlist"):
        result["freshness"] = "stale"
    elif seconds_since <= fresh_threshold:
        result["freshness"] = "fresh"
    elif seconds_since <= stale_threshold:
        result["freshness"] = "degraded"
    else:
        result["freshness"] = "stale"
    return result


def _hls_playlist_url(playlist: Path, *, output_root: Path, channel_slug: str) -> str:
    channel_root = (output_root / channel_slug).resolve(strict=False)
    resolved_playlist = playlist.resolve(strict=False)
    try:
        relative_playlist = resolved_playlist.relative_to(channel_root)
    except ValueError:
        relative_playlist = Path(playlist.name)
    relative_url = quote(PurePosixPath(*relative_playlist.parts).as_posix(), safe="/")
    return f"/hls/{quote(channel_slug, safe='')}/{relative_url}"


def _health_payload(runtime: Mapping[str, Any]) -> dict[str, Any]:
    service = runtime.get("service") if isinstance(runtime.get("service"), Mapping) else {}
    block = runtime.get("block") if isinstance(runtime.get("block"), Mapping) else {}
    ffmpeg = runtime.get("ffmpeg") if isinstance(runtime.get("ffmpeg"), Mapping) else {}
    hls = runtime.get("hls") if isinstance(runtime.get("hls"), Mapping) else {}
    freshness = hls.get("freshness")
    checks = {
        "service_state": "ok" if service.get("status") in {"running", "recovering", "complete"} else "error",
        "active_block_present": "ok" if block.get("current") else "error",
        "ffmpeg_running": "ok" if ffmpeg.get("state") == "running" else ("degraded" if ffmpeg.get("state") in {"unknown", "exited"} else "error"),
        "playlist_present": "ok" if freshness not in {"missing", "unreadable"} else "error",
        "playlist_updating": "ok" if freshness == "fresh" else ("degraded" if freshness == "degraded" else "error"),
        "hls_freshness": "ok" if freshness == "fresh" else ("degraded" if freshness == "degraded" else "error"),
    }
    if any(value == "error" for value in checks.values()):
        status = "error"
    elif any(value == "degraded" for value in checks.values()):
        status = "degraded"
    else:
        status = "ok"
    return {
        "channel_id": runtime.get("channel", {}).get("id") if isinstance(runtime.get("channel"), Mapping) else _channel_id(DEFAULT_CHANNEL_SLUG),
        "status": status,
        "checks": checks,
        "details": {"seconds_since_last_segment": hls.get("seconds_since_last_segment"), "media_sequence": hls.get("media_sequence")},
        "updated_at": service.get("updated_at"),
    }


def _epg_payload(status_payload: Mapping[str, Any], *, channel_slug: str = DEFAULT_CHANNEL_SLUG) -> dict[str, Any]:
    channel = _channel_metadata(channel_slug=channel_slug, name=str(status_payload.get("channel") or DEFAULT_CHANNEL_NAME))
    return {"channel": channel, "programmes": _programme_rows(status_payload, channel_id=channel["id"])}


def _programme_rows(status_payload: Mapping[str, Any], *, channel_id: str) -> list[dict[str, Any]]:
    blocks: list[Mapping[str, Any]] = []
    active = status_payload.get("active_block")
    if isinstance(active, Mapping):
        blocks.append(active)
    upcoming = status_payload.get("upcoming_blocks")
    if isinstance(upcoming, list):
        blocks.extend(block for block in upcoming if isinstance(block, Mapping))
    if not blocks:
        schedule = _derived_schedule_payload(status_payload, channel_slug=DEFAULT_CHANNEL_SLUG)
        active = schedule.get("active_block")
        if isinstance(active, Mapping):
            blocks.append(active)
        raw_upcoming = schedule.get("upcoming_blocks")
        if isinstance(raw_upcoming, list):
            blocks.extend(block for block in raw_upcoming if isinstance(block, Mapping))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for block in blocks:
        start = block.get("start_time")
        stop = block.get("end_time")
        title = str(block.get("title") or "untitled")
        if not start or not stop:
            continue
        identity = (start, stop, title)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({"channel_id": channel_id, "title": title, "start_time": start, "end_time": stop})
    return rows


def _m3u_payload(*, base_url: str) -> str:
    channel = _channel_metadata()
    hls_url = f"{base_url}/hls/{channel['slug']}/{channel['slug']}.m3u8"
    return "\n".join([
        "#EXTM3U",
        f"#EXTINF:-1 tvg-id=\"{channel['id']}\" tvg-name=\"{channel['name']}\" tvg-logo=\"\" group-title=\"FS42\",{channel['name']}",
        hls_url,
        "",
    ])


def _xmltv_payload(status_payload: Mapping[str, Any]) -> str:
    channel = _channel_metadata(name=str(status_payload.get("channel") or DEFAULT_CHANNEL_NAME))
    tv = ET.Element("tv", {"generator-info-name": "fs42stream"})
    channel_element = ET.SubElement(tv, "channel", {"id": channel["id"]})
    ET.SubElement(channel_element, "display-name").text = channel["name"]
    for row in _programme_rows(status_payload, channel_id=channel["id"]):
        start = _xmltv_time(row.get("start_time"))
        stop = _xmltv_time(row.get("end_time"))
        if not start or not stop:
            continue
        programme = ET.SubElement(tv, "programme", {"start": start, "stop": stop, "channel": channel["id"]})
        ET.SubElement(programme, "title").text = str(row.get("title") or "untitled")
    xml = ET.tostring(tv, encoding="unicode", short_empty_elements=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n"


def _xmltv_time(value: Any) -> str | None:
    dt = _parse_iso_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S %z")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _derived_schedule_payload(status_payload: Mapping[str, Any], *, channel_slug: str) -> dict[str, Any]:
    events = status_payload.get("events")
    event_list = [event for event in events if isinstance(event, Mapping)] if isinstance(events, list) else []
    block_events: list[dict[str, Any]] = []
    for event in event_list:
        block = event.get("block")
        if not isinstance(block, Mapping):
            continue
        block_events.append({
            "event": event.get("event"),
            "block_number": event.get("block_number"),
            "block": dict(block),
            "plan_item_count": event.get("plan_item_count"),
            "playlist": event.get("playlist"),
        })

    active_event = _last_block_start_event(block_events) or (block_events[-1] if block_events else None)
    active_index = _block_identity(active_event.get("block")) if active_event else None
    previous_blocks: list[dict[str, Any]] = []
    upcoming_blocks: list[dict[str, Any]] = []
    seen_previous: set[tuple[Any, Any]] = set()
    seen_upcoming: set[tuple[Any, Any]] = set()
    for item in block_events:
        block = item.get("block")
        if not isinstance(block, Mapping):
            continue
        identity = _block_identity(block)
        normalized = dict(block)
        if active_index is not None and identity == active_index:
            continue
        if active_event is not None and item is active_event:
            continue
        if active_index is not None and _block_sort_key(identity) < _block_sort_key(active_index):
            if identity not in seen_previous:
                previous_blocks.append(normalized)
                seen_previous.add(identity)
        elif identity not in seen_upcoming:
            upcoming_blocks.append(normalized)
            seen_upcoming.add(identity)

    playlist = status_payload.get("playlist")
    if not playlist:
        raw_hls = status_payload.get("hls")
        if isinstance(raw_hls, Mapping):
            playlist = raw_hls.get("playlist")

    raw_active_block = status_payload.get("active_block")
    if isinstance(raw_active_block, Mapping):
        active_block = dict(raw_active_block)
    else:
        active_block = dict(active_event.get("block")) if active_event and isinstance(active_event.get("block"), Mapping) else None
    raw_upcoming_blocks = status_payload.get("upcoming_blocks")
    if isinstance(raw_upcoming_blocks, list):
        upcoming_blocks = [dict(block) for block in raw_upcoming_blocks if isinstance(block, Mapping)]
    timeline = _derive_plan_timeline(active_block) if isinstance(active_block, Mapping) else []
    schedule_now = status_payload.get("schedule_now") or status_payload.get("updated_at")
    current_plan_item = _current_plan_item(timeline, schedule_now)
    return {
        "source": "fs42stream-status",
        "channel": status_payload.get("channel") or DEFAULT_CHANNEL_NAME,
        "slug": channel_slug,
        "status": status_payload.get("status"),
        "schedule_now": schedule_now,
        "active_block": active_block,
        "previous_blocks": previous_blocks,
        "upcoming_blocks": upcoming_blocks,
        "timeline": timeline,
        "current_plan_item": current_plan_item,
        "recent_events": block_events[-10:],
        "hls": {
            "playlist": playlist,
            "channel_output_dir": status_payload.get("channel_output_dir"),
            "output_root": status_payload.get("output_root"),
        },
    }


def _derive_plan_timeline(active_block: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_start = _parse_iso_datetime(active_block.get("start_time"))
    plan = active_block.get("plan")
    if raw_start is None or not isinstance(plan, list):
        return []
    cursor = raw_start
    timeline: list[dict[str, Any]] = []
    for index, item in enumerate(plan):
        if not isinstance(item, Mapping):
            continue
        duration = _float_or_zero(item.get("duration"))
        wallclock_start = cursor
        wallclock_end = wallclock_start + timedelta(seconds=duration)
        skip = _float_or_zero(item.get("skip"))
        timeline.append(
            {
                "index": index,
                "content_type": item.get("content_type"),
                "media_type": item.get("media_type"),
                "path": item.get("path"),
                "duration": duration,
                "skip": skip,
                "is_stream": item.get("is_stream"),
                "wallclock_start": _format_datetime(wallclock_start),
                "wallclock_end": _format_datetime(wallclock_end),
                "media_seek_start": skip,
                "media_seek_end": skip + duration,
            }
        )
        cursor = wallclock_end
    return timeline


def _current_plan_item(timeline: Sequence[Mapping[str, Any]], schedule_now: Any) -> dict[str, Any] | None:
    now = _parse_iso_datetime(schedule_now)
    if now is None:
        return None
    for item in timeline:
        start = _parse_iso_datetime(item.get("wallclock_start"))
        end = _parse_iso_datetime(item.get("wallclock_end"))
        if start is None or end is None:
            continue
        if start <= now < end:
            current = dict(item)
            offset = (now - start).total_seconds()
            current["current_offset_in_item"] = offset
            current["media_seek"] = _float_or_zero(item.get("media_seek_start")) + offset
            return current
    return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_datetime(value: datetime) -> str:
    return value.isoformat()


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _last_block_start_event(block_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(block_events):
        if event.get("event") == "block_start":
            return event
    return None


def _block_identity(block: Any) -> tuple[Any, Any]:
    if isinstance(block, Mapping):
        return block.get("index"), block.get("start_time")
    return None, None


def _block_sort_key(identity: tuple[Any, Any]) -> tuple[int, str]:
    index, start_time = identity
    if isinstance(index, int):
        return index, str(start_time or "")
    return 10**9, str(start_time or "")


def _hls_content_type(path: Path) -> str:
    if path.suffix == ".m3u8":
        return "application/vnd.apple.mpegurl"
    if path.suffix == ".ts":
        return "video/mp2t"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def create_server(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, output_root: Path | str = DEFAULT_OUTPUT_ROOT, status_json: Path | str | None = None) -> FS42APIHTTPServer:
    output_root_path = Path(output_root)
    status_json_path = Path(status_json) if status_json is not None else None
    config = APIServerConfig(host=host, port=port, output_root=output_root_path, status_json=status_json_path)
    output_root_path.mkdir(parents=True, exist_ok=True)
    return FS42APIHTTPServer((host, port), FS42APIRequestHandler, config)


def main(argv: Sequence[str] | None = None, *, server_factory: Callable[..., FS42APIHTTPServer] = create_server) -> int:
    parser = argparse.ArgumentParser(description="Run the foreground FS42-Stream API/status/HLS server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--status-json", type=Path, default=None)
    args = parser.parse_args(argv)

    server = server_factory(host=args.host, port=args.port, output_root=args.output_root, status_json=args.status_json)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
